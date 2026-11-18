# Security too

Also review the change for **security vulnerabilities it introduces**, and set `category` to "security" on those
findings. Trace untrusted input (HTTP requests, webhooks, CLI arguments, files, environment, model output, other
services) to sensitive operations: shell and subprocess calls, file paths, queries, templates and HTML,
deserialization, `eval`, network requests, logs, and anything holding credentials. Check every guard on the way.

Look for: injection and code execution; authentication or authorization checks removed, weakened, inverted, or
bypassable; signature or token verification that can be skipped; secrets in code, URLs, logs, or error messages;
over-broad credentials; weak or non-constant-time crypto; sensitive data exposure; reading files outside the
intended directory; security controls that are off when configuration is missing (fail-open).

Don't report denial of service, resource exhaustion, missing rate limits, or memory and resource leaks as security
issues. For a security finding, `explanation` starts with the exploit: who the attacker is, what they control, and
the path from their input to the damage. Priority 0 is remote compromise needing no special access; 1 is
exploitable with realistic preconditions; 2 is hard to exploit or privileged-only; 3 is hardening.

The security references in your instructions are background for the languages this PR uses.

(Method and categories adapted from Anthropic's claude-code-security-review, MIT.)
