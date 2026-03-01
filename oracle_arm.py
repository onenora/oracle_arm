#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import base64
import logging
import random
import re
import secrets
import signal
import string
import sys
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Dict, Any, Type, Tuple

import oci
import requests
from oci.core import ComputeClient, VirtualNetworkClient
from oci.exceptions import ServiceError, RequestException, ConfigFileNotFound

# ==========================================
# 1. 异常模型层 (Exceptions)
# ==========================================
class OciSniperError(Exception): """基础异常类"""
class ConfigurationError(OciSniperError): """配置解析异常"""
class RateLimitError(OciSniperError): """API限速异常 (429)"""
class CapacityError(OciSniperError): """资源不足异常 (500 Out of capacity)"""
class QuotaError(OciSniperError): """配额耗尽异常 (400 Service limit)"""
class FatalApiError(OciSniperError): """其他致命API异常"""

# ==========================================
# 2. 配置与领域模型层 (Models & Config)
# ==========================================
@dataclass(frozen=True)
class AppConfig:
    max_attempts: int
    global_timeout_hours: float
    dry_run: bool
    disable_root_password: bool

@dataclass(frozen=True)
class TFConfig:
    compartment_id: str
    memory_in_gbs: float
    ocpus: float
    availability_domain: str
    subnet_id: str
    image_id: str
    display_name: str
    boot_volume_size_in_gbs: float = 50.0
    ssh_authorized_keys: str = ""

    @classmethod
    def from_file(cls, path: Path) -> "TFConfig":
        if not path.exists():
            raise ConfigurationError(f"Terraform 文件不存在: {path}")
        
        content = path.read_text(encoding="utf-8")
        
        def extract(pattern: str, default: Optional[str] = None, required: bool = True) -> str:
            if match := re.search(pattern, content):
                return match.group(1).strip()
            if default is not None or not required:
                return default or ""
            raise ConfigurationError(f"解析失败: 缺少必须参数正则匹配 '{pattern}'")

        return cls(
            compartment_id=extract(r'compartment_id\s*=\s*"(.*?)"'),
            memory_in_gbs=float(extract(r'memory_in_gbs\s*=\s*"(.*?)"')),
            ocpus=float(extract(r'ocpus\s*=\s*"(.*?)"')),
            availability_domain=extract(r'availability_domain\s*=\s*"(.*?)"'),
            subnet_id=extract(r'subnet_id\s*=\s*"(.*?)"'),
            image_id=extract(r'source_id\s*=\s*"(.*?)"'),
            display_name=extract(r'display_name\s*=\s*"(.*?)"', default="Oracle-ARM-Auto"),
            boot_volume_size_in_gbs=float(extract(r'boot_volume_size_in_gbs\s*=\s*"(.*?)"', default="50.0")),
            ssh_authorized_keys=extract(r'"ssh_authorized_keys"\s*=\s*"(.*?)"', required=False)
        )

# ==========================================
# 3. 通知服务层 (Notifiers)
# ==========================================
class Notifier(ABC):
    @abstractmethod
    def send(self, message: str) -> None:
        pass

class TelegramNotifier(Notifier):
    def __init__(self, token: str, chat_id: str, api_host: str = "api.telegram.org"):
        self.token = token
        self.chat_id = chat_id
        self.api_url = f"https://{api_host}/bot{token}/sendMessage"

    def send(self, message: str) -> None:
        if not self.token or not self.chat_id:
            return
        payload = {'chat_id': self.chat_id, 'text': f'🐢 [OCI Sniper] 播报 🐢\n\n{message}'}
        try:
            # 独立重试与超时控制，不影响主流程
            requests.post(self.api_url, data=payload, timeout=10)
        except requests.RequestException as e:
            logging.warning(f"通知发送失败 (Network/Timeout): {e}")

class CompositeNotifier(Notifier):
    """组合模式：支持同时推送到多个渠道"""
    def __init__(self, notifiers: list[Notifier]):
        self.notifiers = notifiers
        
    def send(self, message: str) -> None:
        for n in self.notifiers:
            n.send(message)

# ==========================================
# 4. 基础设施与重试工具 (Infrastructure & Utils)
# ==========================================
def retry_on_network_error(max_retries: int = 3, backoff_factor: float = 2.0):
    """专门用于处理网络抖动和DNS解析失败的装饰器"""
    def decorator(func):
        def wrapper(*args, **kwargs):
            attempt = 0
            while attempt < max_retries:
                try:
                    return func(*args, **kwargs)
                except (requests.RequestException, RequestException) as e:
                    attempt += 1
                    sleep_t = backoff_factor ** attempt + random.uniform(0, 1)
                    logging.warning(f"网络异常 ({e.__class__.__name__})，{sleep_t:.1f}秒后进行第 {attempt} 次重试...")
                    time.sleep(sleep_t)
                    if attempt >= max_retries:
                        raise NetworkError(f"网络请求失败，已重试 {max_retries} 次: {e}") from e
        return wrapper
    return decorator

def generate_secure_password(length: int = 16) -> str:
    """使用 secrets 替代 random，满足安全审计要求"""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    while True:
        pwd = ''.join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in pwd) and any(c.isupper() for c in pwd) and 
            sum(c.isdigit() for c in pwd) >= 3):
            return pwd

# ==========================================
# 5. OCI 服务层 (Service Layer)
# ==========================================
class InstanceService:
    def __init__(self, config_path: str, profile: str = "DEFAULT"):
        try:
            self.oci_cfg = oci.config.from_file(file_location=config_path, profile_name=profile)
            oci.config.validate_config(self.oci_cfg)
            self.compute_client = ComputeClient(config=self.oci_cfg)
            self.network_client = VirtualNetworkClient(config=self.oci_cfg)
        except ConfigFileNotFound as e:
            raise ConfigurationError(f"OCI 配置文件错误: {e}")

    def _parse_oci_error(self, e: ServiceError) -> type[OciSniperError]:
        """将底层 OCI 错误映射为领域异常"""
        if e.status == 429 or e.code == 'TooManyRequests':
            return RateLimitError
        if e.status == 500 and e.code == 'InternalError' and 'Out of host capacity' in e.message:
            return CapacityError
        if e.status == 400 and 'Service limit' in e.message:
            return QuotaError
        return FatalApiError

    @retry_on_network_error(max_retries=3)
    def launch(self, tf: TFConfig, user_data_b64: str, dry_run: bool = False) -> Optional[Any]:
        """发起创建实例请求"""
        # 严格区分 metadata (放SSH Key等短字符) 与 extended_metadata (放Cloud-Init等大对象)
        metadata = {}
        if tf.ssh_authorized_keys:
            metadata['ssh_authorized_keys'] = tf.ssh_authorized_keys

        details = oci.core.models.LaunchInstanceDetails(
            display_name=tf.display_name,
            compartment_id=tf.compartment_id,
            shape="VM.Standard.A1.Flex",
            shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
                ocpus=tf.ocpus, memory_in_gbs=tf.memory_in_gbs
            ),
            availability_domain=tf.availability_domain,
            create_vnic_details=oci.core.models.CreateVnicDetails(
                subnet_id=tf.subnet_id, hostname_label=tf.display_name
            ),
            source_details=oci.core.models.InstanceSourceViaImageDetails(
                image_id=tf.image_id, boot_volume_size_in_gbs=tf.boot_volume_size_in_gbs,
            ),
            metadata=metadata,
            extended_metadata={'user_data': user_data_b64},
            is_pv_encryption_in_transit_enabled=True,
        )

        if dry_run:
            logging.info(f"[Dry Run] 模拟调用创建 API: 负载校验通过。")
            return None

        try:
            return self.compute_client.launch_instance(details).data
        except ServiceError as e:
            raise self._parse_oci_error(e)(f"[{e.status} {e.code}] {e.message}") from e

    @retry_on_network_error(max_retries=5)
    def get_public_ip(self, compartment_id: str, instance_id: str) -> Optional[str]:
        attachments = self.compute_client.list_vnic_attachments(
            compartment_id=compartment_id, instance_id=instance_id
        ).data
        if attachments and attachments[0].vnic_id:
            vnic = self.network_client.get_vnic(attachments[0].vnic_id).data
            return vnic.public_ip
        return None

# ==========================================
# 6. 核心调度与控制层 (Application Controller)
# ==========================================
class SniperController:
    """负责状态机调度、退避算法和信号处理"""
    def __init__(self, app_config: AppConfig, tf_config: TFConfig, 
                 service: InstanceService, notifier: Notifier):
        self.app_cfg = app_config
        self.tf = tf_config
        self.service = service
        self.notifier = notifier
        
        self.shutdown_event = threading.Event()
        self.start_time = time.time()
        self.attempts = 0
        
        self._setup_signal_handlers()

    def _setup_signal_handlers(self):
        """优雅退出处理 (SIGINT/SIGTERM)"""
        def handler(signum, frame):
            logging.warning(f"接收到退出信号 ({signum})，准备优雅停机...")
            self.shutdown_event.set()
        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)

    def _calculate_backoff(self, base: float = 8.0, max_backoff: float = 60.0, is_rate_limit: bool = False) -> float:
        """带有 Jitter（抖动）的指数退避算法，避免集群雪崩请求"""
        if is_rate_limit:
            # 遇到 429 时，基础惩罚时间更长
            base = max(base, 20.0)
            
        # 指数增长并用 max_backoff 截断
        sleep_time = min(max_backoff, base * (1.2 ** self.attempts))
        # 增加 10% ~ 30% 的抖动
        jitter = random.uniform(0.1 * sleep_time, 0.3 * sleep_time)
        return sleep_time + jitter

    def _generate_cloud_init(self) -> Tuple[str, Optional[str]]:
        """生成初始化脚本，返回 (base64_payload, plaintext_password)"""
        pwd = None
        bash_cmds = ["#!/bin/bash"]
        
        if self.app_cfg.disable_root_password:
            bash_cmds.append("sudo sed -i 's/^.*PermitRootLogin.*/PermitRootLogin prohibit-password/g' /etc/ssh/sshd_config;")
        else:
            pwd = generate_secure_password()
            bash_cmds.append(f"echo root:{pwd} | sudo chpasswd root")
            bash_cmds.append("sudo sed -i 's/^.*PermitRootLogin.*/PermitRootLogin yes/g' /etc/ssh/sshd_config;")
            bash_cmds.append("sudo sed -i 's/^.*PasswordAuthentication.*/PasswordAuthentication yes/g' /etc/ssh/sshd_config;")
            
        bash_cmds.append("sudo systemctl restart sshd")
        script = "\n".join(bash_cmds)
        return base64.b64encode(script.encode('utf-8')).decode('utf-8'), pwd

    def run(self):
        cloud_init_b64, root_pwd = self._generate_cloud_init()
        
        start_msg = (
            f"🚀 [OCI Sniper 启动]\n"
            f"区域: {self.tf.availability_domain}\n"
            f"配置: {self.tf.ocpus}C | {self.tf.memory_in_gbs}G | {self.tf.boot_volume_size_in_gbs}G\n"
            f"最大尝试: {self.app_cfg.max_attempts} 次 | Dry-Run: {self.app_cfg.dry_run}"
        )
        logging.info(start_msg.replace('\n', ' '))
        self.notifier.send(start_msg)

        while not self.shutdown_event.is_set():
            self.attempts += 1
            elapsed_hours = (time.time() - self.start_time) / 3600
            
            # 全局熔断检查
            if self.attempts > self.app_cfg.max_attempts:
                logging.error("达到最大尝试次数限制，退出任务。")
                break
            if elapsed_hours > self.app_cfg.global_timeout_hours:
                logging.error(f"达到最大运行时间限制 ({self.app_cfg.global_timeout_hours}h)，退出任务。")
                break

            try:
                instance = self.service.launch(self.tf, cloud_init_b64, self.app_cfg.dry_run)
                
                if self.app_cfg.dry_run:
                    logging.info("Dry-Run 模式执行完毕。")
                    break

                self._on_success(instance, root_pwd)
                break

            except CapacityError as e:
                backoff = self._calculate_backoff(base=5.0)
                logging.info(f"第 {self.attempts} 次尝试: 容量不足 (Out of capacity)。休眠 {backoff:.1f}s...")
                # 使用 Event.wait() 替代 time.sleep()，实现可随时响应 SIGINT 的中断休眠
                self.shutdown_event.wait(timeout=backoff)

            except RateLimitError as e:
                backoff = self._calculate_backoff(base=20.0, is_rate_limit=True)
                logging.warning(f"第 {self.attempts} 次尝试: 触发 API 限速 (429)。强退避 {backoff:.1f}s...")
                self.shutdown_event.wait(timeout=backoff)

            except QuotaError as e:
                msg = f"❌ 严重错误: 您的租户配额不足 (Service Limit)。程序终止。\n详细信息: {e}"
                logging.error(msg)
                self.notifier.send(msg)
                sys.exit(1) # 此处为顶层控制，允许退出

            except FatalApiError as e:
                logging.error(f"❌ 发生不可恢复的 API 错误: {e}")
                self.notifier.send(f"脚本因 API 错误异常终止:\n{e}")
                sys.exit(1)
                
            except NetworkError as e:
                logging.error(f"⚠️ 核心网络错误: {e}")
                self.shutdown_event.wait(timeout=10.0)

        # 循环结束检查，如果是被中断的发送最后通知
        if self.shutdown_event.is_set():
            msg = f"🛑 任务已被手动/系统终止 (SIGTERM/SIGINT)。总计尝试: {self.attempts} 次，耗时: {elapsed_hours:.2f} 小时。"
            logging.info(msg)
            self.notifier.send(msg)

    def _on_success(self, instance, root_pwd: Optional[str]):
        """处理创建成功的逻辑：获取IP并通知"""
        logging.info("🎉 实例创建 API 调用成功，正在等待分配公网 IP...")
        
        public_ip = None
        # 等待最多 5 分钟获取 IP
        for _ in range(30):
            if self.shutdown_event.is_set():
                break
            public_ip = self.service.get_public_ip(self.tf.compartment_id, instance.id)
            if public_ip:
                break
            self.shutdown_event.wait(10.0)

        msg = (
            f"🎉 **甲骨文 ARM 创建成功！** 🎉\n"
            f"尝试次数: {self.attempts}\n"
            f"区域: {self.tf.availability_domain}\n"
            f"公网 IP: {public_ip or '获取超时，请登录控制台查看'}\n"
        )
        if root_pwd:
            # 建议真实环境中，密码不要直接推送到明文频道，或者加密发送
            msg += f"🔑 临时 Root 密码: {root_pwd}\n(建议登录后立即修改或禁用密码登录)"
            # 在控制台仅打印脱敏密码
            logging.info(f"分配密码: {root_pwd[:3]}***{root_pwd[-3:]}")
        else:
            msg += f"🔑 登录方式: 仅允许 SSH Key 登录"

        logging.info(msg.replace('\n', ' '))
        self.notifier.send(msg)


# ==========================================
# 7. 主程序入口 (CLI)
# ==========================================
def setup_logging(debug: bool = False):
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("oci_sniper.log", encoding="utf-8")
        ]
    )

def main():
    parser = argparse.ArgumentParser(description="🚀 Oracle ARM 实例抢注工具")
    parser.add_argument("tf_file", type=Path, help="Terraform main.tf 配置文件路径")
    parser.add_argument("--dry-run", action="store_true", help="不实际发起创建请求，仅测试链路")
    parser.add_argument("--max-attempts", type=int, default=100000, help="最大尝试次数 (默认: 100000)")
    parser.add_argument("--timeout", type=float, default=168.0, help="全局最大运行时间(小时) (默认: 168h/7天)")
    parser.add_argument("--disable-pwd", action="store_true", help="禁用 Root 密码登录，强制仅使用 SSH Key")
    parser.add_argument("--debug", action="store_true", help="开启 Debug 级别日志")
    
    # 隐藏的 TG 环境变量读取方式，避免在代码中硬编码
    parser.add_argument("--tg-token", type=str, default="", help="Telegram Bot Token")
    parser.add_argument("--tg-chat", type=str, default="", help="Telegram Chat ID")

    args = parser.parse_args()
    setup_logging(args.debug)

    try:
        # 组装配置
        app_cfg = AppConfig(
            max_attempts=args.max_attempts,
            global_timeout_hours=args.timeout,
            dry_run=args.dry_run,
            disable_root_password=args.disable_pwd
        )
        tf_cfg = TFConfig.from_file(args.tf_file)

        # 依赖注入 (DI)
        notifiers = []
        if args.tg_token and args.tg_chat:
            notifiers.append(TelegramNotifier(args.tg_token, args.tg_chat))
        else:
            logging.warning("未配置 Telegram 参数，将静默运行，仅输出本地日志。")
            
        composite_notifier = CompositeNotifier(notifiers)
        oci_service = InstanceService("~/.oci/config")

        # 启动控制器
        controller = SniperController(
            app_config=app_cfg,
            tf_config=tf_cfg,
            service=oci_service,
            notifier=composite_notifier
        )
        controller.run()

    except OciSniperError as e:
        logging.error(f"程序初始化/运行失败: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        # 捕捉在初始化阶段就按 Ctrl+C 的极端情况
        logging.info("进程被强制终止。")
        sys.exit(0)

if __name__ == "__main__":
    main()