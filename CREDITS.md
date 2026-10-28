# Credits

Tessera-CT is original work. Some of its ideas came from other people's public writing, which we rebuilt in our own code and words.

## Where it came from

Tessera-CT grew out of Ken Bandoly's own custom harness, one that largely built and evolved itself. Self-evolution and self-improvement were the core of the project from the very beginning. It has many possible uses, and we've left how to apply it, self-improvement included, up to each adopter.

## Victor Taelin: OptChat / UniiChat

- Gist: https://gist.github.com/VictorTaelin/91837951a5ce5b38f341ec1ba1df6449 (X: @VictorTaelin)
- Earlier work: OptMem, https://github.com/VictorTaelin/OptMem

The continuous thread is built on the idea in that gist: the chat itself is the memory. These ideas come from him:

- An append-only log of typed messages.
- A binary tree of fixed-size one-line summaries.
- A view that is fine near now and coarse in the past, merged in batches so the prompt cache holds.
- A persisted view that is never rebuilt from the log.
- `zoom` down to the raw message.
- Compaction calls that reuse the turn's prompt, with a byte ruler and retries.
- Subagents that report back as one message.

The gist has no license, so we took the design only. The prompts, code and docs are our own. The AetherMind engine's summary tree follows the same spec.

## A nod to Fred Nix (@NixFred)

Thanks to Fred Nix (@NixFred) for Larry, his field guide to building a persistent AI teammate: https://how.nixfred.com/. It's a project we respect, and it was good company while we polished this one for release.

## Thanks to poteto and pstack

Thanks to poteto for pstack, the practice stack we built with: small named principles, a playbook the agent walks, and a check that proves the path. It shaped how this project was built and reviewed, and it earned its place here.

## Thanks to the local AI community

- **Mia's AI Lab** (https://mia-ai.net/, X: @MiaAI_lab) for the great open local hosting recipes.
- **Ash Hart** (X: @ashxhart) for **TensorFold** (https://tensorfold.dev/, source: https://github.com/ashhart/TensorFold), the local inference engine.

This project would not have been possible without their work. Contributions from a community this good are what make open source so rewarding, and possible.

If you see your work here and want the credit worded differently, please open an issue.
