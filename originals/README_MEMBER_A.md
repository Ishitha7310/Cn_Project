# Member A application code

This bundle contains the five Member A application files:
- app/make_content.py
- app/server.py
- app/client.py
- app/workload.py
- app/plot_results.py

IMPORTANT:
The client, workload, content-generator, and plotting files are recovered from the prior conversation's code. `app/server.py` is reconstructed from the implementation features recorded in the conversation, not guaranteed byte-for-byte identical to the original server file you tested. Compare it with your `~/jackfruit_sdn_backup.zip` before replacing your original.

Run from the project root with the `venv313` environment active:
    python app/make_content.py
    python app/server.py --id srv1
    python app/client.py --file small.bin
    python app/workload.py --clients 3 --requests 3 --files small.bin medium.bin large.bin
    python app/plot_results.py

Dependencies for plotting:
    python -m pip install pandas matplotlib
