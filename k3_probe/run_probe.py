import asyncio
import json
import os
import time

from crucible.gateway import GatewayClient


async def run():
    with open("k3_probe/snapshot.json") as f:
        snapshot = json.load(f)
    with open("k3_probe/diagnosis_prompt.txt") as f:
        template = f.read()

    prompt = template.replace("{{SNAPSHOT}}", json.dumps(snapshot, indent=2))

    client = GatewayClient()
    result = await client.chat(
        prompt=prompt,
        system="You are a performance engineering assistant. Return only valid JSON.",
        request={
            "provider": os.getenv("CRUCIBLE_GATEWAY_PROVIDER", "gemini"),
            "agent": "crucible_agent",
            "session": f"spike-k3-{int(time.time())}",
        }
    )
    await client.close()

    # result["text"] is the model's response
    content = result["text"].strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    try:
        diagnosis = json.loads(content)
        parse_ok = True
    except json.JSONDecodeError as e:
        diagnosis = {"raw": result["text"], "parse_error": str(e)}
        parse_ok = False

    output = {
        "timestamp": time.time(),
        "provider": result.get("provider"),
        "model": result.get("model"),
        "input_tokens": result.get("input_tokens", 0),
        "output_tokens": result.get("output_tokens", 0),
        "cost_usd": (result.get("input_tokens", 0) * 0.000001) +
                    (result.get("output_tokens", 0) * 0.000002),
        "parse_ok": parse_ok,
        "diagnosis": diagnosis,
    }

    path = f"k3_probe/result_{int(time.time())}.json"
    os.makedirs("k3_probe", exist_ok=True)
    with open(path, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\n[K3] Result saved to {path}")
    print(f"[K3] Parse OK: {parse_ok}")
    if parse_ok:
        print(f"[K3] Primary cause: {diagnosis.get('primary_cause')}")
        print(f"[K3] Proposed change: {diagnosis.get('proposed_change')}")
        print(f"[K3] Confidence: {diagnosis.get('confidence')}")
    else:
        print(f"[K3] RAW:\n{result['text']}")

if __name__ == "__main__":
    asyncio.run(run())
