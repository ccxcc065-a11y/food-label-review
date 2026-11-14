"""v2 C：任务级计时与调用记录（UTC 时间戳 + 单调计时器，每任务独立）。"""
import datetime
import time
import uuid


class TaskTimer:
    def __init__(self):
        self.taskId = "T" + uuid.uuid4().hex[:16]
        self.startedUtc = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")
        self._start = time.monotonic()
        self.stages = []
        self.calls = []
        self.marks = {}

    def mark(self, name):
        now = time.monotonic()
        self.stages.append({"stage": name, "atMs": round((now - self._start) * 1000, 1),
                            "utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")})
        self.marks[name] = now
        return now

    def model(self, role, attempt, usage, status, provider=None, model=None, duration_ms=None):
        self.calls.append({"role": role, "attempt": attempt, "usage": usage or {},
                           "status": status, "provider": provider or {}, "model": model or "",
                           "atMs": round((time.monotonic() - self._start) * 1000, 1),
                           "durationMs": round(duration_ms, 1) if duration_ms is not None else None})

    def ready(self, name):
        if name not in self.marks:
            self.mark(name)
        return round((self.marks[name] - self._start) * 1000, 1)

    def snapshot(self, pdf_ready_ms=None, client_timing=None):
        render_ms = None
        if "render-start" in self.marks and "html-rendered" in self.marks:
            render_ms = round((self.marks["html-rendered"] - self.marks["render-start"]) * 1000, 1)
        return {"taskId": self.taskId, "startedUtc": self.startedUtc,
                "totalMs": round((time.monotonic() - self._start) * 1000, 1),
                "htmlReadyMs": self.ready("html-saved"),
                "renderMs": render_ms,
                "pdfReadyMs": pdf_ready_ms,
                "clientTiming": client_timing or {},
                "stages": list(self.stages), "calls": list(self.calls)}
