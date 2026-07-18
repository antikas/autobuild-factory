#!/usr/bin/env python3
"""
Claude subscription quota probe.

Reads the LIVE utilization of your Claude subscription limits (5-hour session,
weekly-all, per-model weekly like Fable, plus extra-usage credits) from the same
internal endpoint the Claude Code client uses:

    GET https://api.anthropic.com/api/oauth/usage
    Authorization: Bearer <claudeAiOauth.accessToken from ~/.claude/.credentials.json>

WHY this exists: there is NO documented/supported API for subscription limit %.
This endpoint is what the client's own "Account & Usage" dialog calls. It is
UNDOCUMENTED and unsupported -- treat any failure as "unknown"
and never hard-depend on it (autobuild uses it as a soft signal, non-fatal).

SECURITY: the OAuth token is read from disk into memory and passed to curl as a
header; it is NEVER printed or logged. Transport is curl --ssl-no-revoke (tolerant of
machines with TLS-intercepting endpoint protection, where pure-python TLS is unreliable).

USAGE:
    python quota.py            # human summary
    python quota.py --json     # machine-readable normalised object (autobuild reads this)
    python quota.py --gate weekly_all:85   # exit 2 if that limit's percent >= 85 (shell gating)

Output object (--json):
    { "ok": bool, "error": str|None,
      "limits": [ {kind, group, model, percent, severity, resets_at, is_active} ],
      "worst": {kind, percent, severity, resets_at} | None,   # highest-severity limit
      "extra_usage": {enabled, percent, used, limit, currency} | None,
      "fetched_at_epoch": int }
"""
from __future__ import annotations
import json, os, subprocess, sys, time

CRED = os.path.expanduser("~/.claude/.credentials.json")
ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
_SEV_RANK = {"normal": 0, "warning": 1, "critical": 2}


def _token() -> str | None:
    try:
        with open(CRED, encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:
        return None
    c = d.get("claudeAiOauth") or {}
    tok = c.get("accessToken")
    return tok if isinstance(tok, str) and tok else None


def fetch(now: int | None = None) -> dict:
    """Fetch + normalise. Pure-ish: `now` injectable for tests. Never raises."""
    now = now if now is not None else int(time.time())
    tok = _token()
    if not tok:
        return {"ok": False, "error": "no claudeAiOauth token in ~/.claude/.credentials.json",
                "limits": [], "worst": None, "extra_usage": None, "fetched_at_epoch": now}
    try:
        proc = subprocess.run(
            ["curl", "-s", "--ssl-no-revoke", "--max-time", "8", ENDPOINT,
             "-H", f"Authorization: Bearer {tok}",
             "-H", "anthropic-beta: oauth-2025-04-20",
             "-H", "anthropic-version: 2023-06-01",
             "-H", "User-Agent: claude-cli/quota-probe"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"curl failed: {e}", "limits": [], "worst": None,
                "extra_usage": None, "fetched_at_epoch": now}
    if proc.returncode != 0 or not proc.stdout.strip():
        return {"ok": False, "error": f"http/transport (rc={proc.returncode}) {proc.stderr[:120]}",
                "limits": [], "worst": None, "extra_usage": None, "fetched_at_epoch": now}
    try:
        raw = json.loads(proc.stdout)
    except Exception:
        return {"ok": False, "error": "non-JSON response (token expired? endpoint changed?)",
                "limits": [], "worst": None, "extra_usage": None, "fetched_at_epoch": now}

    limits = []
    for lim in (raw.get("limits") or []):
        scope = lim.get("scope") or {}
        model = ((scope.get("model") or {}).get("display_name")) if isinstance(scope, dict) else None
        limits.append({
            "kind": lim.get("kind"), "group": lim.get("group"), "model": model,
            "percent": lim.get("percent"), "severity": lim.get("severity"),
            "resets_at": lim.get("resets_at"), "is_active": bool(lim.get("is_active")),
        })
    worst = max(limits, key=lambda x: (_SEV_RANK.get(x.get("severity"), 0), x.get("percent") or 0),
                default=None)
    eu = raw.get("extra_usage") or {}
    extra = None
    if eu.get("is_enabled"):
        extra = {"enabled": True, "percent": eu.get("utilization"),
                 "used": eu.get("used_credits"), "limit": eu.get("monthly_limit"),
                 "currency": eu.get("currency")}
    return {"ok": True, "error": None, "limits": limits,
            "worst": {k: worst.get(k) for k in ("kind", "percent", "severity", "resets_at")} if worst else None,
            "extra_usage": extra, "fetched_at_epoch": now}


def _human(d: dict) -> str:
    if not d["ok"]:
        return f"quota: UNKNOWN ({d['error']}) -- treat as no-signal (autobuild proceeds)."
    out = ["Claude subscription quota (live):"]
    for l in d["limits"]:
        tag = f" [{l['model']}]" if l.get("model") else ""
        act = "  <== ACTIVE/binding" if l.get("is_active") else ""
        out.append(f"  {str(l['kind'])+tag:<22} {str(l['percent'])+'%':>5}  {l['severity']:<9} resets {l['resets_at']}{act}")
    if d["extra_usage"]:
        e = d["extra_usage"]
        out.append(f"  extra-usage credits    {e['percent']:.0f}%  ({e['used']}/{e['limit']} {e['currency']})")
    return "\n".join(out)


def main(argv: list[str]) -> int:
    as_json = "--json" in argv
    gate = None
    for a in argv:
        if a.startswith("--gate"):
            # --gate weekly_all:85  (next arg or =value)
            v = a.split("=", 1)[1] if "=" in a else (argv[argv.index(a) + 1] if argv.index(a) + 1 < len(argv) else "")
            if ":" in v:
                gate = (v.split(":")[0], float(v.split(":")[1]))
    d = fetch()
    if as_json:
        print(json.dumps(d, ensure_ascii=True))
    else:
        print(_human(d))
    if gate and d["ok"]:
        kind, thresh = gate
        hit = any((l.get("kind") == kind and (l.get("percent") or 0) >= thresh) for l in d["limits"])
        return 2 if hit else 0
    return 0 if d["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
