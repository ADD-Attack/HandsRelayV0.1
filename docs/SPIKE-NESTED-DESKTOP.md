# Spike: nested desktop and host/app split

**Status:** research-only assessment; neither mode is implemented.

## Nested desktop

A nested session could isolate agent input from the owner's visible desktop, but it would also create a second Windows session with different foreground, display, clipboard, credential, and process-visibility behavior. The relay's hands are deliberately attached to the logged-in interactive desktop; running them in a service or disconnected session would make screenshot and input results diverge from what the owner sees. A VM or Remote Desktop session would therefore need its own explicit lifecycle, authentication, screen transport, and clear indication of which desktop is being driven.

**Recommendation:** do not add nested-desktop operation to this relay. If isolation becomes a product requirement, evaluate it as a separately provisioned host with an explicit owner-visible session boundary, not as a hidden mode of the current hands process.

## Host/app agent split

The current boundary is already narrow: an authenticated gateway exposes typed operations; the hands service owns desktop access; the parser is optional and has no public listener by default. Moving agent reasoning into a host-side process would place model credentials and orchestration on the same machine as the desktop-control capability. Keeping the agent outside the host reduces local secret exposure, while the gateway token remains the single agent-facing credential.

**Recommendation:** keep the agent external and the host limited to the gateway, hands, and optional parser. Any future local-agent proposal must specify credential storage, update ownership, process isolation, and whether a compromised agent can bypass the gateway's operation validation.

## Decision

Neither proposal is justified by current requirements. Retain the existing one-owner-session desktop contract and revisit only with a specific isolation need and a reviewed threat model.
