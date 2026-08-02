from contextvars import ContextVar


request_id_var = ContextVar("request_id", default="-")


def get_request_id() -> str:
    return request_id_var.get()
