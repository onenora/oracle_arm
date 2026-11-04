#!/usr/bin/env python3
"""Oracle Cloud ARM (VM.Standard.A1.Flex) 实例抢注脚本。"""
import argparse
import base64
import json
import logging
import os
import random
import re
import secrets
import shlex
import signal
import string
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

import oci
from oci.exceptions import ServiceError

SHAPE = "VM.Standard.A1.Flex"
MAX_INTERVAL = 120.0
RESULT_FILE = Path(__file__).with_name("instance.json")

log = logging.getLogger("oracle_arm")


class ConfigError(Exception):
    pass


# ---------------------------------------------------------------------------
# main.tf 解析
# ---------------------------------------------------------------------------
def _instance_block(text: str) -> str:
    """截取 resource "oci_core_instance" 的块体，避免匹配到 VCN/子网等其他资源的同名字段。"""
    m = re.search(r'resource\s+"oci_core_instance"\s+"[^"]*"\s*\{', text)
    if not m:
        return text
    depth, i = 1, m.end()
    while i < len(text) and depth:
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        i += 1
    return text[m.end():i - 1]


def _top_level(block: str) -> str:
    """去掉嵌套块，只保留块体顶层的属性。"""
    out, depth = [], 0
    for c in block:
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif depth == 0:
            out.append(c)
    return "".join(out)


def _hostname_label(name: str) -> str:
    label = re.sub(r"[^a-zA-Z0-9-]+", "-", name).strip("-")[:63].rstrip("-")
    if not label or not label[0].isalpha():
        label = ("arm-" + label)[:63].rstrip("-")
    return label


@dataclass(frozen=True)
class TFConfig:
    compartment_id: str
    availability_domain: str
    subnet_id: str
    image_id: str
    ocpus: float
    memory_in_gbs: float
    display_name: str
    boot_volume_size_in_gbs: float
    ssh_authorized_keys: str
    assign_public_ip: bool

    @classmethod
    def from_file(cls, path: Path) -> "TFConfig":
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            raise ConfigError(f"读取 {path} 失败: {e}") from e

        block = _instance_block(text)
        top = _top_level(block)

        def find(src: str, key: str, quoted: bool = True) -> str | None:
            pat = rf'"?\b{key}"?\s*=\s*"([^"]*)"' if quoted else rf'\b{key}\s*=\s*"?([\d.]+)"?'
            m = re.search(pat, src)
            return m.group(1).strip() if m else None

        def require(value: str | None, key: str, ocid: bool = False) -> str:
            if not value:
                raise ConfigError(f"main.tf 中缺少 {key}")
            if ocid and not value.startswith("ocid1."):
                raise ConfigError(f"{key} 不是 OCID: {value!r}（创建实例时请选择已有 VCN/子网再导出堆栈）")
            return value

        def number(key: str, default: float | None = None) -> float:
            raw = find(block, key, quoted=False)
            if raw is None:
                if default is None:
                    raise ConfigError(f"main.tf 中缺少 {key}")
                return default
            return float(raw)

        return cls(
            compartment_id=require(find(top, "compartment_id"), "compartment_id", ocid=True),
            availability_domain=require(find(top, "availability_domain"), "availability_domain"),
            subnet_id=require(find(block, "subnet_id"), "subnet_id", ocid=True),
            image_id=require(find(block, "source_id"), "source_id", ocid=True),
            ocpus=number("ocpus"),
            memory_in_gbs=number("memory_in_gbs"),
            display_name=find(top, "display_name") or "oracle-arm",
            boot_volume_size_in_gbs=number("boot_volume_size_in_gbs", 50.0),
            ssh_authorized_keys=find(block, "ssh_authorized_keys") or "",
            assign_public_ip=(find(block, "assign_public_ip") or "true").lower() != "false",
        )


# ---------------------------------------------------------------------------
# 通知
# ---------------------------------------------------------------------------
class Telegram:
    def __init__(self, token: str, chat_id: str, api_host: str = "api.telegram.org"):
        self.enabled = bool(token and chat_id)
        self.chat_id = chat_id
        self.url = f"https://{api_host}/bot{token}/sendMessage"

    def send(self, text: str) -> None:
        if not self.enabled:
            return
        body = json.dumps({"chat_id": self.chat_id, "text": f"🐢 甲骨文 ARM 抢注播报 🐢\n\n{text}"}).encode()
        req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"})
        for i in range(3):
            try:
                urllib.request.urlopen(req, timeout=10).close()
                return
            except urllib.error.HTTPError as e:
                log.warning("Telegram 推送失败: HTTP %s %s", e.code, e.read()[:200].decode(errors="replace"))
                return
            except OSError as e:
                # 只打印类型，避免把含 token 的 URL 写进日志
                log.warning("Telegram 推送网络异常 (%s)，第 %d 次", type(e).__name__, i + 1)
                time.sleep(2)


# ---------------------------------------------------------------------------
# cloud-init
# ---------------------------------------------------------------------------
def gen_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    while True:
        pwd = "".join(secrets.choice(alphabet) for _ in range(length))
        if any(c.islower() for c in pwd) and any(c.isupper() for c in pwd) and any(c.isdigit() for c in pwd):
            return pwd


def build_user_data(root_password: str) -> str:
    # Ubuntu 22.04+ 的 /etc/ssh/sshd_config.d/*-cloudimg-settings.conf 会把 PasswordAuthentication 设为 no，
    # sshd 取首次出现的值，所以写一个排序最靠前的 00- 文件覆盖；同时改主配置兼容没有 Include 的系统。
    script = f"""#!/bin/bash
echo {shlex.quote('root:' + root_password)} | chpasswd
mkdir -p /etc/ssh/sshd_config.d
printf 'PermitRootLogin yes\\nPasswordAuthentication yes\\n' > /etc/ssh/sshd_config.d/00-oracle-arm.conf
sed -i -E 's/^#?[[:space:]]*PermitRootLogin.*/PermitRootLogin yes/; s/^#?[[:space:]]*PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config
systemctl restart ssh 2>/dev/null || systemctl restart sshd
"""
    return base64.b64encode(script.encode()).decode()


# ---------------------------------------------------------------------------
# OCI
# ---------------------------------------------------------------------------
class OCI:
    def __init__(self, config_file: str, profile: str):
        cfg = oci.config.from_file(file_location=config_file, profile_name=profile)
        oci.config.validate_config(cfg)
        self.compute = oci.core.ComputeClient(cfg)
        self.network = oci.core.VirtualNetworkClient(cfg)
        self.identity = oci.identity.IdentityClient(cfg)

    def availability_domains(self, compartment_id: str) -> list[str]:
        return [ad.name for ad in self.identity.list_availability_domains(compartment_id).data]

    def check(self, tf: TFConfig) -> None:
        """只读调用，验证凭据与镜像/子网 OCID。"""
        image = self.compute.get_image(tf.image_id).data
        subnet = self.network.get_subnet(tf.subnet_id).data
        log.info("凭据有效 | 镜像: %s | 子网: %s", image.display_name, subnet.display_name)

    def launch(self, details, retry_token: str):
        # 关闭 SDK 默认重试：否则 500 Out of host capacity 会在 SDK 内部重试数分钟，节奏由脚本自己控制。
        return self.compute.launch_instance(
            details, opc_retry_token=retry_token, retry_strategy=oci.retry.NoneRetryStrategy()
        ).data

    def instance_state(self, instance_id: str) -> str:
        return self.compute.get_instance(instance_id).data.lifecycle_state

    def public_ip(self, compartment_id: str, instance_id: str) -> str | None:
        for att in self.compute.list_vnic_attachments(compartment_id=compartment_id, instance_id=instance_id).data:
            if att.vnic_id:
                ip = self.network.get_vnic(att.vnic_id).data.public_ip
                if ip:
                    return ip
        return None


def build_launch_details(tf: TFConfig, ad: str, user_data: str | None):
    metadata = {}
    if tf.ssh_authorized_keys:
        metadata["ssh_authorized_keys"] = tf.ssh_authorized_keys
    if user_data:
        metadata["user_data"] = user_data
    m = oci.core.models
    return m.LaunchInstanceDetails(
        display_name=tf.display_name,
        compartment_id=tf.compartment_id,
        availability_domain=ad,
        shape=SHAPE,
        shape_config=m.LaunchInstanceShapeConfigDetails(ocpus=tf.ocpus, memory_in_gbs=tf.memory_in_gbs),
        create_vnic_details=m.CreateVnicDetails(subnet_id=tf.subnet_id, assign_public_ip=tf.assign_public_ip,
                                                hostname_label=_hostname_label(tf.display_name)),
        source_details=m.InstanceSourceViaImageDetails(image_id=tf.image_id,
                                                       boot_volume_size_in_gbs=tf.boot_volume_size_in_gbs),
        metadata=metadata,
        is_pv_encryption_in_transit_enabled=True,
    )


def classify(e: ServiceError) -> str:
    msg = (e.message or "").lower()
    if e.status == 429:
        return "ratelimit"
    if e.status >= 500 or e.status == 409 or "capacity" in msg:
        return "retry"
    if e.code in ("LimitExceeded", "QuotaExceeded") or "limit" in msg or "quota" in msg:
        return "quota"
    return "fatal"


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------
class Sniper:
    def __init__(self, args, tf: TFConfig, api: OCI, tg: Telegram):
        self.args, self.tf, self.api, self.tg = args, tf, api, tg
        self.stop = threading.Event()
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)

    def _on_signal(self, signum, _frame):
        log.warning("收到信号 %s，准备退出", signum)
        self.stop.set()

    def notify(self, text: str, level: int = logging.INFO) -> None:
        log.log(level, text.replace("\n", " | "))
        self.tg.send(text)

    def run(self) -> int:
        tf, args = self.tf, self.args
        password = None if args.disable_pwd else gen_password()
        user_data = build_user_data(password) if password else None
        ads = [tf.availability_domain]
        if args.all_ads:
            # main.tf 里的可用域排第一，其余按区域顺序轮询
            ads += [ad for ad in self.api.availability_domains(tf.compartment_id) if ad != tf.availability_domain]
        details = [(ad, build_launch_details(tf, ad, user_data)) for ad in ads]

        self.notify(f"启动\n可用域: {', '.join(ads)}\n实例: {tf.display_name}\n"
                    f"配置: {tf.ocpus:g}C / {tf.memory_in_gbs:g}G / {tf.boot_volume_size_in_gbs:g}G\n"
                    f"登录: {'仅 SSH Key' if args.disable_pwd else 'root 密码 + SSH Key'}")

        if args.dry_run:
            self.api.check(tf)
            log.info("Dry-run 完成，未发起创建请求")
            return 0

        start, attempts, interval, token, idx = time.time(), 0, args.interval, None, 0
        while not self.stop.is_set():
            if attempts >= args.max_attempts or time.time() - start > args.timeout * 3600:
                self.notify(f"达到上限（{attempts} 次 / {(time.time() - start) / 3600:.1f}h），未抢到，退出",
                            logging.WARNING)
                return 1
            attempts += 1
            token = token or uuid.uuid4().hex
            ad, launch_details = details[idx]
            try:
                instance = self.api.launch(launch_details, token)
            except ServiceError as e:
                token = None  # 服务端已处理该请求，下次换 token、换可用域
                idx = (idx + 1) % len(details)
                kind = classify(e)
                detail = f"[{ad}] {e.status} {e.code}: {e.message}"
                if kind == "ratelimit":
                    interval = min(interval * 2, MAX_INTERVAL)
                    log.warning("第 %d 次: 限速 (429)，间隔调至 %.0fs", attempts, interval)
                elif kind == "retry":
                    interval = max(args.interval, interval * 0.8)
                    log.info("第 %d 次: %s", attempts, detail)
                elif kind == "quota":
                    self.notify(f"❌ 配额不足，脚本停止。请到控制台检查是否已有实例或 CPU/内存/硬盘已被占用。\n{detail}",
                                logging.ERROR)
                    return 1
                else:
                    self.notify(f"❌ 不可恢复的错误，脚本停止。请检查 main.tf 与 OCI 配置。\n{detail}", logging.ERROR)
                    return 1
            except Exception as e:  # 网络/超时等：保留 token 重试，防止请求已生效却丢了响应导致重复创建
                log.warning("第 %d 次: 网络异常 %s: %s", attempts, type(e).__name__, e)
            else:
                self.on_success(instance, ad, attempts, password)
                return 0
            self.stop.wait(interval * random.uniform(0.8, 1.2))

        self.notify(f"🛑 手动停止，共尝试 {attempts} 次", logging.WARNING)
        return 130

    def on_success(self, instance, ad: str, attempts: int, password: str | None) -> None:
        # 先落盘、推送，后续查询 IP 出任何问题都不会丢失密码
        record = {"instance_id": instance.id, "availability_domain": ad, "root_password": password,
                  "public_ip": None, "created_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        self.save(record)
        self.notify(f"🎉 第 {attempts} 次尝试创建成功\n可用域: {ad}\n实例 OCID: {instance.id}\n"
                    + (f"root 密码: {password}" if password else "登录方式: 仅 SSH Key"))

        ip, state = None, instance.lifecycle_state
        deadline = time.time() + 600
        while time.time() < deadline and not self.stop.is_set():
            try:
                state = self.api.instance_state(instance.id)
                if state in ("TERMINATING", "TERMINATED"):
                    self.notify(f"⚠️ 实例被回收 ({state})，请重新运行脚本", logging.ERROR)
                    return
                if state == "RUNNING":
                    ip = self.api.public_ip(self.tf.compartment_id, instance.id)
                    if ip:
                        break
            except Exception as e:
                log.warning("查询实例状态失败 %s: %s", type(e).__name__, e)
            self.stop.wait(10)

        if ip:
            self.save(record | {"public_ip": ip})
            self.notify(f"✅ 实例已运行\n公网 IP: {ip}\nssh root@{ip}" if password else
                        f"✅ 实例已运行\n公网 IP: {ip}\nssh -i <私钥> ubuntu@{ip}（用户名视镜像而定）")
        else:
            self.notify(f"实例状态: {state}，10 分钟内未获取到公网 IP，请到控制台查看", logging.WARNING)


    @staticmethod
    def save(record: dict) -> None:
        try:
            RESULT_FILE.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            RESULT_FILE.chmod(0o600)
        except OSError as e:
            log.error("写入 %s 失败: %s", RESULT_FILE, e)


def main() -> int:
    p = argparse.ArgumentParser(description="Oracle ARM 实例抢注脚本")
    p.add_argument("tf_file", type=Path, help="从堆栈导出的 main.tf")
    p.add_argument("--dry-run", action="store_true", help="只校验配置与凭据，不创建实例")
    p.add_argument("--disable-pwd", action="store_true", help="不设置 root 密码，仅用 main.tf 里的 SSH Key 登录")
    p.add_argument("--all-ads", action="store_true", help="轮询区域内所有可用域（多 AD 区域如 Ashburn/Phoenix/Frankfurt）")
    p.add_argument("--force", action="store_true", help=f"忽略已存在的 {RESULT_FILE.name} 继续抢")
    p.add_argument("--interval", type=float, default=10.0, help="基础请求间隔秒数，遇 429 自动加倍 (默认 10)")
    p.add_argument("--max-attempts", type=int, default=1_000_000, help="最大尝试次数")
    p.add_argument("--timeout", type=float, default=24 * 30, help="最长运行小时数 (默认 720)")
    p.add_argument("--oci-config", default="~/.oci/config", help="OCI 配置文件路径")
    p.add_argument("--profile", default="DEFAULT", help="OCI 配置 profile")
    p.add_argument("--tg-token", default=os.environ.get("TG_BOT_TOKEN", ""), help="Telegram Bot Token，或用环境变量 TG_BOT_TOKEN")
    p.add_argument("--tg-chat", default=os.environ.get("TG_CHAT_ID", ""), help="Telegram Chat ID，或用环境变量 TG_CHAT_ID")
    p.add_argument("--tg-api-host", default=os.environ.get("TG_API_HOST", "api.telegram.org"), help="Telegram API 反代域名")
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stdout)

    if RESULT_FILE.exists() and not args.force and not args.dry_run:
        log.error("%s 已存在，说明之前已抢到实例。确认要再抢一台请删除该文件或加 --force", RESULT_FILE)
        return 2

    try:
        tf = TFConfig.from_file(args.tf_file)
        if args.disable_pwd and not tf.ssh_authorized_keys:
            raise ConfigError("--disable-pwd 需要 main.tf 中包含 ssh_authorized_keys，否则创建后无法登录")
        if not tf.ssh_authorized_keys:
            log.warning("main.tf 中没有 SSH 公钥，只能用 root 密码登录")
        api = OCI(args.oci_config, args.profile)
    except (ConfigError, oci.exceptions.ClientError) as e:
        log.error("初始化失败: %s", e)
        return 2

    tg = Telegram(args.tg_token, args.tg_chat, args.tg_api_host)
    if not tg.enabled:
        log.warning("未配置 Telegram，结果只写入日志和 %s", RESULT_FILE.name)

    try:
        return Sniper(args, tf, api, tg).run()
    except ServiceError as e:  # dry-run 校验 / 获取可用域失败
        log.error("OCI 调用失败: %s %s: %s", e.status, e.code, e.message)
        return 1


if __name__ == "__main__":
    sys.exit(main())
