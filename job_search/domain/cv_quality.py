"""Structure checks and attribution for generated CVs, driven by the profile's ``CvRules``."""

import re

_ATTRIBUTION_SECTION = re.compile(r"\n## AI Generation Attribution\n.*?(?=\n## |\Z)", re.DOTALL)
_WORD = re.compile(r"\b[\w][\w'/-]*\b")
_DUPLICATE_BULLET_OVERLAP = 0.72


def add_model_attribution(markdown, model, generation_date):
    """Replace any attribution section with one naming the model that actually ran."""
    section = (
        "## AI Generation Attribution\n\n"
        f"AI generated this artifact from human-authored source material on {generation_date} using `{model}`."
    )
    return _ATTRIBUTION_SECTION.sub("", markdown).rstrip() + "\n\n" + section + "\n"


def _has_duplicate_bullets(markdown):
    bullets = [
        re.findall(r"[a-z0-9]+", line.lower()) for line in markdown.splitlines() if line.lstrip().startswith("-")
    ]
    for index, left in enumerate(bullets):
        if len(left) < 8:
            continue
        left_set = set(left)
        for right in bullets[index + 1 :]:
            if len(right) < 8:
                continue
            overlap = len(left_set & set(right)) / min(len(left_set), len(set(right)))
            if overlap >= _DUPLICATE_BULLET_OVERLAP:
                return True
    return False


def cv_style_issues(markdown, rules, master_resume=""):
    """Return high-signal structural problems that justify one repair pass (empty when the CV passes)."""
    issues = [f"missing required section: {heading}" for heading in rules.required_sections if heading not in markdown]
    title_lines = [
        re.sub(r"^#{1,6}\s+", "", line.strip()).lower() for line in markdown.splitlines()[:8] if line.strip()
    ]
    if not any(line.startswith(("curriculum vitae \u2014", "curriculum vitae -")) for line in title_lines):
        issues.append("title must use the Curriculum Vitae document-title formulation")
    words = len(_WORD.findall(markdown))
    if words < rules.min_words:
        issues.append(f"too short ({words} words; target at least {rules.min_words})")
    if len(re.findall(r"^#### ", markdown, flags=re.MULTILINE)) < rules.min_subsections:
        issues.append(
            f"needs at least {rules.min_subsections} thematic subsections under the most recent or most relevant role"
        )
    lower = markdown.lower()
    issues.extend(
        f"must not contain provenance or placeholder phrase: {phrase}"
        for phrase in rules.forbidden_phrases
        if phrase in lower
    )
    for employer in re.findall(r"^## (.+?) \(redact\)\s*$", master_resume, flags=re.MULTILINE | re.IGNORECASE):
        if employer.lower() in lower:
            issues.append(f"must not include redacted employer: {employer}")
    issues.extend(f"must preserve: {needed}" for needed in rules.must_include if needed.lower() not in lower)
    issues.extend(
        f"must preserve the documented {need} for {when}"
        for when, need in rules.required_with
        if when in lower and need not in lower
    )
    if _has_duplicate_bullets(markdown):
        issues.append("contains effectively duplicate bullets that should be merged or differentiated")
    return issues
