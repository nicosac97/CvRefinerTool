"""One live Jev call covering all three primitives. Prints raw answers and cost."""

import pprint

from typesafe_sdk import TypeSafeClient

from cvrefine.config import settings

STATE = """CANDIDATE: Cloud developer, 5 years, AWS, Terraform, Python, Kubernetes. Based in Italy.

JOB: Senior Cloud Engineer at Acme. Remote within EU, quarterly on-site in Berlin.
Requirements: AWS, Terraform, CI/CD, Kubernetes. Nice to have: Go."""

client = TypeSafeClient(
    api_key=settings.openrouter_api_key,
    base_url=settings.openrouter_base_url,
)

result = client.system_one(
    model=settings.decision_model,
    state=STATE,
    questions={
        "italy_ok": {"type": "noul", "instructions": "Can a candidate living in Italy take this job?"},
        "work_mode": {
            "type": "choice",
            "instructions": "What is the work mode of this job?",
            "criteria": {
                "remote": "Work from home, at most occasional travel",
                "hybrid": "Regular weekly or monthly office days",
                "onsite": "Office based",
                "unclear": "Not stated",
            },
        },
        "skills_fit": {
            "type": "score",
            "instructions": "How well do the candidate's skills match the job requirements?",
            "criteria": [
                "No required skill present",
                "Few required skills present",
                "About half of required skills present",
                "Most required skills present",
                "All required skills present",
            ],
        },
    },
)

print("type:", type(result))
pprint.pprint(result.model_dump() if hasattr(result, "model_dump") else vars(result))
