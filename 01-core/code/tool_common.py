# ================================================================================
# tool_common.py
#
# Shared plumbing for the agent's action-group (tool) Lambdas. Bedrock invokes a
# function-schema action group with a fixed event shape and expects a fixed
# response shape; these two helpers hide that boilerplate so each tool handler is
# just "read params → do the AWS call → return text".
# ================================================================================


def parse_params(event):
    """Return the agent-supplied parameters as a {name: value} dict.

    Bedrock passes every parameter value as a STRING regardless of declared type,
    so callers coerce (e.g. "true" → bool) themselves.
    """
    return {p.get("name"): p.get("value") for p in event.get("parameters", [])}


def respond(event, text):
    """Wrap a plain-text result in the response envelope Bedrock expects back."""
    return {
        "messageVersion": "1.0",
        "response": {
            "actionGroup": event.get("actionGroup"),
            "function":    event.get("function"),
            "functionResponse": {
                "responseBody": {"TEXT": {"body": str(text)}}
            },
        },
    }


def as_bool(value, default=False):
    """Coerce a Bedrock string parameter to a bool."""
    if value is None:
        return default
    return str(value).strip().lower() in ("true", "1", "yes")
