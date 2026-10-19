# 应用层入口转发核心配装偏好校验，避免 storage 依赖 application。
from echo_workshop.core.allocation_preferences import (
    allocation_settings,
    pending_role_keys,
)

__all__ = ['allocation_settings', 'pending_role_keys']
