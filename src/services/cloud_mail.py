"""
CloudMail 邮箱服务实现
基于 CloudMail REST API (https://doc.skymail.ink/api/api-doc.html)
"""

import re
import time
import logging
import random
import string
from typing import Optional, Dict, Any, List

from .base import BaseEmailService, EmailServiceError, EmailServiceType
from ..core.http_client import HTTPClient, RequestConfig
from ..config.constants import OTP_CODE_PATTERN

logger = logging.getLogger(__name__)


class CloudMailService(BaseEmailService):
    """
    CloudMail 邮箱服务
    基于 CloudMail REST API
    """

    def __init__(self, config: Dict[str, Any] = None, name: str = None):
        """
        初始化 CloudMail 服务

        Args:
            config: 配置字典，支持以下键:
                - base_url: API 基础地址 (必需)
                - admin_email: 管理员邮箱 (必需)
                - admin_password: 管理员密码 (必需)
                - domain: 邮箱域名 (必需)
                - timeout: 请求超时时间 (默认: 30)
                - max_retries: 最大重试次数 (默认: 3)
                - proxy_url: 代理 URL
            name: 服务名称
        """
        super().__init__(EmailServiceType.CLOUD_MAIL, name)

        required_keys = ["base_url", "admin_email", "admin_password", "domain"]
        missing_keys = [key for key in required_keys if not (config or {}).get(key)]
        if missing_keys:
            raise ValueError(f"缺少必需配置: {missing_keys}")

        default_config = {
            "timeout": 30,
            "max_retries": 3,
            "proxy_url": None,
        }
        self.config = {**default_config, **(config or {})}
        self.config["base_url"] = self.config["base_url"].rstrip("/")

        http_config = RequestConfig(
            timeout=self.config["timeout"],
            max_retries=self.config["max_retries"],
        )
        self.http_client = HTTPClient(
            proxy_url=self.config.get("proxy_url"),
            config=http_config,
        )

        # 认证 token 缓存
        self._token: Optional[str] = None
        self._emails_cache: Dict[str, Dict[str, Any]] = {}

    def _ensure_token(self):
        """确保已获取有效的认证 token"""
        if self._token:
            return

        url = f"{self.config['base_url']}/api/public/genToken"
        try:
            response = self.http_client.request("POST", url, json={
                "email": self.config["admin_email"],
                "password": self.config["admin_password"],
            }, headers={"Content-Type": "application/json", "Accept": "application/json"})

            if response.status_code >= 400:
                raise EmailServiceError(f"获取 token 失败: HTTP {response.status_code} - {response.text[:200]}")

            data = response.json()
            if data.get("code") != 200:
                raise EmailServiceError(f"获取 token 失败: {data.get('message', '未知错误')}")

            token = data.get("data", {}).get("token") if isinstance(data.get("data"), dict) else data.get("data")
            if not token:
                raise EmailServiceError("获取 token 失败: 返回数据中无 token")

            self._token = token
            logger.info("CloudMail token 获取成功")

        except Exception as e:
            self.update_status(False, e)
            if isinstance(e, EmailServiceError):
                raise
            raise EmailServiceError(f"获取 token 失败: {e}")

    def _get_headers(self) -> Dict[str, str]:
        """获取带认证的请求头"""
        self._ensure_token()
        return {
            "Authorization": self._token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _make_request(self, method: str, path: str, retry_on_auth: bool = True, **kwargs) -> Dict[str, Any]:
        """
        发送 API 请求

        Args:
            method: HTTP 方法
            path: 请求路径
            retry_on_auth: 认证失败时是否重试
            **kwargs: 请求参数

        Returns:
            响应 JSON 数据

        Raises:
            EmailServiceError: 请求失败
        """
        url = f"{self.config['base_url']}{path}"
        kwargs.setdefault("headers", {})
        kwargs["headers"].update(self._get_headers())

        try:
            response = self.http_client.request(method, url, **kwargs)

            if response.status_code == 401 and retry_on_auth:
                # token 过期，重新获取
                self._token = None
                return self._make_request(method, path, retry_on_auth=False, **kwargs)

            if response.status_code >= 400:
                error_msg = f"API 请求失败: HTTP {response.status_code}"
                try:
                    error_data = response.json()
                    error_msg = f"{error_msg} - {error_data}"
                except Exception:
                    error_msg = f"{error_msg} - {response.text[:200]}"
                self.update_status(False, EmailServiceError(error_msg))
                raise EmailServiceError(error_msg)

            data = response.json()
            if data.get("code") != 200:
                raise EmailServiceError(f"API 错误: {data.get('message', '未知错误')}")

            return data

        except Exception as e:
            self.update_status(False, e)
            if isinstance(e, EmailServiceError):
                raise
            raise EmailServiceError(f"API 请求失败: {method} {path} - {e}")

    def create_email(self, config: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        通过 addUser API 创建邮箱账户

        Args:
            config: 配置参数:
                - name: 邮箱前缀（可选，不提供则随机生成）
                - domain: 邮箱域名（可选，默认使用配置中的域名）

        Returns:
            包含邮箱信息的字典:
            - email: 邮箱地址
            - service_id: 同 email
            - id: 同 email
        """
        req_config = config or {}
        domain = req_config.get("domain") or self.config.get("domain")

        prefix = req_config.get("name")
        if not prefix:
            prefix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=10))

        email_address = f"{prefix}@{domain}"

        # 生成随机密码
        password = ''.join(random.choices(
            string.ascii_letters + string.digits, k=16
        ))

        try:
            self._make_request("POST", "/api/public/addUser", json={
                "list": [
                    {
                        "email": email_address,
                        "password": password,
                    }
                ]
            })

            email_info = {
                "email": email_address,
                "service_id": email_address,
                "id": email_address,
                "created_at": time.time(),
                "domain": domain,
                "password": password,
            }

            self._emails_cache[email_address] = email_info

            logger.info(f"成功创建 CloudMail 邮箱: {email_address}")
            self.update_status(True)
            return email_info

        except Exception as e:
            self.update_status(False, e)
            if isinstance(e, EmailServiceError):
                raise
            raise EmailServiceError(f"创建邮箱失败: {e}")

    def get_verification_code(
        self,
        email: str,
        email_id: str = None,
        timeout: int = 120,
        pattern: str = OTP_CODE_PATTERN,
        otp_sent_at: Optional[float] = None,
    ) -> Optional[str]:
        """
        从 CloudMail 获取验证码

        Args:
            email: 邮箱地址
            email_id: 未使用
            timeout: 超时时间（秒）
            pattern: 验证码正则
            otp_sent_at: OTP 发送时间戳

        Returns:
            验证码字符串，超时返回 None
        """
        logger.info(f"正在从 CloudMail 邮箱 {email} 获取验证码...")

        start_time = time.time()
        seen_email_ids: set = set()

        while time.time() - start_time < timeout:
            try:
                data = self._make_request("POST", "/api/public/emailList", json={
                    "toEmail": email,
                    "type": 0,  # 收件
                    "isDel": 0,  # 未删除
                    "timeSort": "desc",
                    "num": 1,
                    "size": 20,
                })

                mails = data.get("data", [])
                if not isinstance(mails, list):
                    time.sleep(3)
                    continue

                for mail in mails:
                    mail_id = mail.get("emailId")
                    if not mail_id or mail_id in seen_email_ids:
                        continue

                    seen_email_ids.add(mail_id)

                    sender = str(mail.get("sendEmail", "")).lower()
                    subject = str(mail.get("subject", ""))
                    text = str(mail.get("text", ""))
                    html_content = str(mail.get("content", ""))

                    # 简单去除 HTML 标签
                    plain_from_html = re.sub(r"<[^>]+>", " ", html_content) if html_content else ""

                    content = f"{sender}\n{subject}\n{text}\n{plain_from_html}"

                    # 检查是否是 OpenAI 邮件
                    if "openai" not in content.lower():
                        continue

                    # 过滤掉邮箱地址后匹配验证码
                    email_pattern = r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"
                    cleaned = re.sub(email_pattern, "", content)

                    match = re.search(pattern, cleaned)
                    if match:
                        code = match.group(1)
                        logger.info(f"从 CloudMail 邮箱 {email} 找到验证码: {code}")
                        self.update_status(True)
                        return code

            except Exception as e:
                logger.debug(f"检查邮件时出错: {e}")

            time.sleep(3)

        logger.warning(f"等待验证码超时: {email}")
        return None

    def list_emails(self, **kwargs) -> List[Dict[str, Any]]:
        """
        列出邮件（通过 emailList API）

        Returns:
            邮件列表
        """
        try:
            params = {
                "timeSort": "desc",
                "num": kwargs.get("num", 1),
                "size": kwargs.get("size", 50),
                "isDel": 0,
            }
            to_email = kwargs.get("to_email")
            if to_email:
                params["toEmail"] = to_email

            data = self._make_request("POST", "/api/public/emailList", json=params)
            mails = data.get("data", [])
            self.update_status(True)
            return mails if isinstance(mails, list) else []
        except Exception as e:
            logger.warning(f"列出邮件失败: {e}")
            self.update_status(False, e)
            return []

    def delete_email(self, email_id: str) -> bool:
        """
        CloudMail API 不直接支持删除用户，返回 True 并清除缓存

        Args:
            email_id: 邮箱地址

        Returns:
            True
        """
        self._emails_cache.pop(email_id, None)
        logger.info(f"从缓存中移除 CloudMail 邮箱: {email_id}")
        return True

    def check_health(self) -> bool:
        """检查 CloudMail 服务是否可用"""
        try:
            self._token = None  # 强制重新获取 token
            self._ensure_token()
            logger.debug("CloudMail 服务健康检查通过")
            self.update_status(True)
            return True
        except Exception as e:
            logger.warning(f"CloudMail 服务健康检查失败: {e}")
            self.update_status(False, e)
            return False
