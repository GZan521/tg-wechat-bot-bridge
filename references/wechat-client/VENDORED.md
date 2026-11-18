# Vendored WeChat client

This folder is a self-contained snapshot of the WeChat iLink bridge client, so a deployment of this skill no longer needs to clone any external repository.

- Source: an open source WeChat iLink bridge project (MIT), snapshot copied on 2026-11-18.
- License: MIT, see the LICENSE file in this folder. The copyright notice of the original project is preserved there.
- Local changes against the source snapshot: the WELCOME_MESSAGES in ilink.py are this deployment's custom welcome text, and the version-check and update endpoints inside ilink.py were repointed from the source project to this skill's own repository (GZan521/tg-wechat-bot-bridge), so the vendored copy runs independently and no longer contacts the source project. Everything else is byte-identical to the source.
- Contents: ilink.py (the protocol client), daemon.py (the example alternative driver), requirements.txt, AGENTS.md (the deployment contract, maintained with the code), and examples/muse-hook (the production hook driver scripts and their README).
- Updates from the upstream project no longer flow in automatically. To take an upstream fix, diff the new upstream ilink.py against this copy, re-apply the welcome message change, and re-verify with a live deployment before shipping.
