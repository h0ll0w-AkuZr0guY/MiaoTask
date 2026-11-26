"""Explicit USB ADB endpoints; network and emulator transports are disabled."""
from dataclasses import dataclass
from typing import Protocol

from backend.core.device.discovery import is_usb_serial


@dataclass(frozen=True)
class DeviceEndpoint:
    serial: str
    driver: str = 'uiautomator2'
    transport: str = 'usb'
    label: str = ''


class EndpointProvider(Protocol):
    def discover(self) -> list[DeviceEndpoint]: ...


class UsbEndpointProvider:
    """Explicit USB serial provider without port scanning or network connect."""

    def __init__(self, serials, label='Android USB 设备'):
        self.endpoints = []
        for serial in serials:
            if not is_usb_serial(serial):
                raise ValueError('仅支持有效的 USB 设备序列号')
            self.endpoints.append(DeviceEndpoint(serial, transport='usb', label=label))

    def discover(self):
        return list(self.endpoints)
