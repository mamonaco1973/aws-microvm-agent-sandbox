# ================================================================================
# tool_get_costs.py  — agent tool: get_month_to_date_cost
#
# Read-only. Returns month-to-date unblended spend via Cost Explorer, optionally
# broken down by service. IAM: ce:GetCostAndUsage only.
# ================================================================================

import logging
from datetime import date, timedelta

import boto3

from tool_common import parse_params, respond, as_bool

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Cost Explorer is a global (us-east-1) service regardless of the app region.
ce = boto3.client("ce", region_name="us-east-1")


def lambda_handler(event, context):
    params    = parse_params(event)
    by_service = as_bool(params.get("group_by_service"), default=False)
    logger.info("get_month_to_date_cost invoked (by_service=%s)", by_service)

    # First of the current month → tomorrow (End is exclusive, so this includes
    # today's partial spend).
    today = date.today()
    start = today.replace(day=1).isoformat()
    end   = (today + timedelta(days=1)).isoformat()

    try:
        kwargs = {
            "TimePeriod": {"Start": start, "End": end},
            "Granularity": "MONTHLY",
            "Metrics": ["UnblendedCost"],
        }
        if by_service:
            kwargs["GroupBy"] = [{"Type": "DIMENSION", "Key": "SERVICE"}]

        result = ce.get_cost_and_usage(**kwargs)
        groups = result["ResultsByTime"][0]

        if by_service:
            lines = []
            total = 0.0
            for g in groups.get("Groups", []):
                svc  = g["Keys"][0]
                amt  = float(g["Metrics"]["UnblendedCost"]["Amount"])
                total += amt
                if amt >= 0.01:
                    lines.append(f"  {svc}: ${amt:,.2f}")
            lines.sort(key=lambda s: float(s.split("$")[1].replace(",", "")), reverse=True)
            body = (f"Month-to-date spend (since {start}): ${total:,.2f}\n"
                    "By service:\n" + ("\n".join(lines) if lines else "  (no charges yet)"))
        else:
            amt  = float(groups["Total"]["UnblendedCost"]["Amount"])
            body = f"Month-to-date spend (since {start}): ${amt:,.2f}"
    except Exception as exc:
        logger.exception("get_cost_and_usage failed")
        body = f"Error retrieving costs: {exc}"

    return respond(event, body)
