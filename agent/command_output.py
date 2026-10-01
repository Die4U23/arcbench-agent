"""Keep actionable test failures when DOM snapshots dominate command output."""
import re


def failure_details(text: str) -> str:
    """Prefer Vitest failure blocks over unrelated passing test output."""
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)
    blocks = re.findall(r'(?m)^\s*FAIL\s+[^\n]+\n[\s\S]*?(?=^\s*⎯|^\s*FAIL\s+|\Z)', text)
    if blocks:
        validation_lines = []
        if text.startswith('Visible validation errors from test DOM:'):
            for line in text.splitlines():
                if re.match(r'\s*(?:>|RUN\b|✓|❯|FAIL\b)', line):
                    break
                validation_lines.append(line)
        validation = '\n'.join(validation_lines)
        return validation + '\n'.join(blocks)
    return '\n'.join(line for line in text.splitlines() if not re.match(r'\s*✓', line))


def compact_command_output(text: str, limit: int) -> str:
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)
    lines = []
    validation = []
    in_dom = False
    for line in text.splitlines():
        if line.startswith('Ignored nodes:'):
            in_dom = True
            continue
        if in_dom and re.match(r'\s*(?:❯|FAIL\b|AssertionError:|Test Files\b|Tests\b|⎯)', line):
            in_dom = False
        if in_dom:
            value = line.strip()
            if '<' not in value and re.search(r'\b(?:required|must|invalid|do not match|expired|too short|too long)\b', value, re.I):
                if value not in validation:
                    validation.append(value[:300])
            continue
        lines.append(line)
    if validation:
        lines.insert(0, 'Visible validation errors from test DOM:\n' + '\n'.join(validation[:30]))
    output = '\n'.join(lines)
    if len(output) <= limit:
        return output
    half = limit // 2
    return output[:half] + '\n...[middle truncated]\n' + output[-half:]
