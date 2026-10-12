"""Chat-timeout contract (retry-treadmill fix).

Local llama.cpp-class servers send zero response bytes until prompt
prefill finishes, so the client READ timeout is what bounds prefill —
a short one kills requests mid-prefill, and the (previously mutating)
retry re-sent a longer prompt into the busy slot, never converging.
"""
import httpx

from backend.agent import model_client


def test_chat_timeout_bounds_read_not_prefill():
    t = model_client._chat_timeout()
    assert t.connect == model_client.CHAT_CONNECT_TIMEOUT
    assert t.write == model_client.CHAT_WRITE_TIMEOUT
    assert t.pool == model_client.CHAT_POOL_TIMEOUT
    # The read timeout must cover a worst-case cold prefill: at ~40 tok/s
    # (CPU-expert MoE), a 128k-token prompt prefills in ~55 minutes.
    assert t.read >= 3600, "read timeout shorter than worst-case prefill re-creates the treadmill"


def test_chat_timeout_is_an_httpx_timeout():
    assert isinstance(model_client._chat_timeout(), httpx.Timeout)
