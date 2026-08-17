# Contributing to breakdown

We welcome feedback and contributions — bug reports, docs fixes, new
providers, statistical improvements, whatever you've got. Please, come on in and open an issue or a
pull request.

You're also welcome to just use breakdown. Run it, point it at your own
metric trees, and use it as part of your company's internal tooling. Our FSL [license](LICENSE) permits any use, commercial or otherwise, except selling it as part of a competing product — and each release converts to plain Apache-2.0 two years after it ships, by the license's own irrevocable grant. Fork it, extend it, adapt it to
what you need. We hope you'll contribute improvements back if you're so inclined.

## Before you open a pull request

We use a lightweight [Individual Contributor License Agreement](CLA.md)
(based on the Apache Software Foundation's ICLA) to keep the copyright and
patent grants on breakdown clear and uniform, for your protection as a
contributor as well as ours. It's standard practice across a lot of open
source projects and doesn't ask you to give up copyright in what you write —
just to confirm you have the right to contribute it and to grant the license
described there.

The first time you open a pull request, a bot will comment asking you to sign
by replying with a fixed phrase. You only need to do this once.

## Getting set up

See [AGENTS.md](AGENTS.md) for how the codebase is organized, including
[how to install dependencies and run the test suite](AGENTS.md#run--test). If you're working on attribution, intervals, or fitting,
read [`knowledge/statistics_whitepaper.md`](knowledge/statistics_whitepaper.md)
first — it's the living record of what the engine's statistics do and don't
guarantee.
