from ipaddress import IPv4Address, IPv6Address
from typing import Optional
from pydantic import BaseModel, Field, model_validator

class JoinRoom(BaseModel):
    name: str = Field(min_length=1, max_length=32)
    ipv4: Optional[IPv4Address] = None
    ipv6: Optional[IPv6Address] = None
    port: int = Field(ge=1, le=65535)

    @model_validator(mode="after")
    def check_ip_present(self):
        if self.ipv4 is None and self.ipv6 is None:
            raise ValueError("Either ipv4 or ipv6 must be provided")
        return self


class NewRoom(JoinRoom):
    token: str
    thumbprint: str

