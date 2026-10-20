"""Keep actionable test failures when DOM snapshots dominate command output."""
import re


def _runtime_failures(text: str) -> str:
    """Select bounded exception messages/frames, never arbitrary log context."""
    error_line = re.compile(
        r'^\s*(?:(?:Caused by|\[cause\]|Unhandled rejection):\s*)?'
        r'(?:[A-Za-z_$][\w.$]*Error|Error)(?:\s+\[[\w_]+\])?:', re.I
    )
    frame_line = re.compile(r'^\s+(?:at\s+|❯\s+).+:\d+(?::\d+)?\)?\s*$')
    lines = text.splitlines()
    snippets = []
    seen = set()
    remaining = 2000
    for index, line in enumerate(lines):
        if not error_line.match(line):
            continue
        snippet = [line.strip()[:400]]
        for frame in lines[index + 1:index + 9]:
            if not frame_line.match(frame):
                break
            snippet.append(frame[:200])
        detail = '\n'.join(snippet)
        if detail in seen:
            continue
        seen.add(detail)
        snippets.append(detail[:remaining])
        remaining -= len(snippets[-1]) + 1
        if remaining <= 0 or len(snippets) >= 4:
            break
    return '\n'.join(snippets)


def failure_details(text: str) -> str:
    """Keep failed assertions and bounded runtime causes, excluding passing tests."""
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)
    failure_block = re.compile(r'(?m)^\s*FAIL\s+[^\n]+\n[\s\S]*?(?=^\s*⎯|^\s*FAIL\s+|\Z)')
    blocks = failure_block.findall(text)
    if blocks:
        validation_lines = []
        if text.startswith('Visible validation errors from test DOM:'):
            for line in text.splitlines():
                if re.match(r'\s*(?:>|RUN\b|✓|❯|FAIL\b)', line):
                    break
                validation_lines.append(line)
        validation = '\n'.join(validation_lines)
        runtime = _runtime_failures(failure_block.sub('\n', text))
        return '\n'.join(part for part in (validation, runtime, '\n'.join(blocks)) if part)
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
