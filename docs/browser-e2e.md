# Dashboard browser verification

On 2026-10-25, the Insight dashboard was opened in a new Chrome window against the real coding session created by `scripts/e2e.py`. The session included an intentional failed Bash assertion, a new `add.py` file, and a passing recovery check.

The bridge ran from the locked all-extras development environment: Python 3.11.15, `agent-trajviz` 0.2.0, Gradio 6.30.0, and Plotly 7.1.0. It served only `127.0.0.1:7899`.

Opening the URL populated the transcript control and rendered the dashboard without clicking **Load Trajectory**. The visible values were 17 records (3 assistant and 14 user/bookkeeping records), 22,970 tokens, one error issue, and 66.7% success across three tool calls. The token and duration plots rendered too. This verifies browser execution of the preload hook, beyond the HTTP/config checks in the automated harness.

The unedited screenshot is retained privately at `/tmp/trajviz-obs-dashboard.png`. It is excluded from the repository because the native browser frame includes personal browser chrome. No model request was made during browser verification. The test window was closed and the dashboard server was stopped with Ctrl-C, exiting successfully.
