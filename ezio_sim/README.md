# Simulated Ezio

`mock_ezio.py` is a local stand-in for Ezio's scoring API, with its scripted answers in
`fixtures.json`. Para starts it automatically unless `EZIO_URL` (and `EZIO_KEY`) point at a live Ezio.
The original buildathon entry in [`../buildathon/`](../buildathon/) uses it too.

`Dockerfile` runs it on its own: `docker build -t ezio-sim ezio_sim && docker run -p 8787:8787 ezio-sim`.
