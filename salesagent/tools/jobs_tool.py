"""job_status — the agent's 'is it done yet?' (free)."""
from __future__ import annotations

from ..artifacts import store
from ..jobs.manager import JobManager
from .envelope import envelope, error_envelope
from .registry import CostClass, tool_spec


@tool_spec(
    name="job_status",
    description=("Check a background job (branch scrape, bulk verification, "
                 "price scrape). When done, the result artifact is surfaced. "
                 "Free."),
    input_schema={"properties": {"job_id": {"type": "string"}},
                  "required": ["job_id"]},
    cost_class=CostClass.FREE,
)
def job_status(ctx, job_id: str) -> dict:
    st = JobManager.get(ctx.settings).status(job_id)
    if not st:
        return error_envelope(f"no job {job_id}")
    if st.get("conversation_id") not in (ctx.conversation_id, None):
        # jobs are org-visible by design; note the origin
        pass
    lines = [f"job **{job_id}** ({st['tool_name']}): **{st['status']}**"]
    if st.get("progress_total"):
        lines.append(f"progress {st['progress_done']}/{st['progress_total']}"
                     + (f" — {st['message']}" if st.get("message") else ""))
    if st.get("error"):
        lines.append(f"error: {st['error']}")
    tail = st.get("log") or []
    if tail:
        lines.append("log tail:")
        lines += [f"- {x['msg']}" for x in tail[-6:]]
    if st["status"] == "done" and st.get("result_artifact_id"):
        spec = store.get(ctx.rw(), st["result_artifact_id"])
        if spec:
            ctx.emit("artifact", {"artifact_id": spec["artifact_id"],
                                  "version": spec["version"],
                                  "kind": spec["kind"], "title": spec["title"]})
            lines.append(f"result artifact: {spec['artifact_id']} "
                         f"({spec['row_count']} rows) — reference or "
                         "transform it as needed")
    return envelope(kind="markdown",
                    summary=f"{job_id}: {st['status']}"
                            + (f" -> {st.get('result_artifact_id')}"
                               if st.get("result_artifact_id") else ""),
                    markdown="\n".join(lines), job_id=job_id)
