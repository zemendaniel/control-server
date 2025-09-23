from fastapi import HTTPException

def choose_peer_ip(peer_a: dict, peer_b: dict) -> str:
    # If both peers have IPv6, use that, else use IPv4

    # Both have IPv6
    if peer_a.get("ipv6") and peer_b.get("ipv6"):
        return peer_a["ipv6"]

    # Both have IPv4
    if peer_a.get("ipv4") and peer_b.get("ipv4"):
        return peer_a["ipv4"]

    # this is not supposed to happen
    raise HTTPException(status_code=409, detail="One peer has IPv6 and the other has IPv4")