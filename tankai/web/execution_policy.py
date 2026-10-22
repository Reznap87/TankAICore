"""Conservative, deterministic selection of the existing verified path."""
import re

DEFAULT_DOD = "Eine klare, überprüfbare Antwort liegt vor."


def execution_path(goal: str, definition_of_done: str) -> str:
    # Only a complete, narrow arithmetic contract is recognized. Additional
    # documents, constraints, research, compound goals or custom DoD fall back
    # to the normal planner. No inference is used for routing.
    arithmetic = re.fullmatch(
        r"\s*Berechne\s+-?\d{1,9}\s+(?:mal|plus|minus|geteilt durch)\s+-?\d{1,9}"
        r"(?:\s+und erkläre den Rechenweg in (?:einem|zwei|drei) kurzen Sätzen auf Deutsch)?"
        r"[.!]?\s*(?:Eine Websuche ist dafür nicht erforderlich\.)?\s*",
        goal, re.IGNORECASE,
    )
    return "verified_single_step" if arithmetic and definition_of_done == DEFAULT_DOD else "standard"
