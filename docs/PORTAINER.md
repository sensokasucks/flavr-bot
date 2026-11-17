# Running FlaVR Bot with Portainer

The stack builds the image from this GitHub repo, so updates are a "Pull and redeploy" (or automatic).

1. Portainer → your environment → **Stacks** → **Add stack**.
2. Name: `flavr-bot`. Build method: **Repository**.
3. Repository URL: `https://github.com/sensokasucks/flavr-bot`, reference `refs/heads/main`,
   compose path `docker-compose.yml`. (A private repo needs **Authentication** with a GitHub
   personal access token that can read it.)
4. **Environment variables** → *Advanced mode* → paste [.env.example](../.env.example) and fill it in.
   At least `DISCORD_TOKEN`, `OWNER_ID` and `FLAVR_CORE_KEY`.
5. Optional: **GitOps updates** on (polling, e.g. every 5 minutes) so a merge into `main` redeploys by itself.
6. **Deploy the stack.** Logs: Containers → flavr-bot → Logs. A healthy start ends with
   `Discord: logged in as FlaVR Bot#…` and `Listening for Stream Core on 0.0.0.0:3870`.

Notes

* The container uses the host's network (`network_mode: host`), so Stream Core reaches it at this
  PC's own address, and `/wake`'s broadcast reaches the home network.
* The database is in the named volume `flavr-bot_flavr-bot-data` (`/data` inside). Redeploys keep it.
* If this PC runs a firewall (ufw), allow the port from the home network only:
  `sudo ufw allow from 192.168.1.0/24 to any port 3870 proto tcp` (use your own network).
* Changing a setting: Stacks → flavr-bot → Environment variables → edit → **Update the stack**.
