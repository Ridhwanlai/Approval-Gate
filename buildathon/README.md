# The original buildathon entry

Para started as an entry in the Decision-Grade AI Buildathon. This folder keeps that entry as it was
judged, so the score on the website can be checked:

- [CHALLENGE.md](CHALLENGE.md): the challenge brief.
- [TEAM_README.md](TEAM_README.md): what the team built and how to run it.
- `gate.py`, `harness/`: the organisers' runner and judge. `wrapper/`: the team's decision wrapper.
- `personas/`, `final/`: the practice and sealed final sets. `results/`: the judged report.

It still runs from this folder, standard library only:

```bash
cd buildathon
python3 gate.py practice --persona paralegal
python3 -m unittest test_contract test_mock test_harness
```

The simulated Ezio it uses is shared with Para, in [`../ezio_sim/`](../ezio_sim/). The product that
grew out of this entry, Para on HarnessRouter, is everything outside this folder; start at the
[main README](../README.md).
