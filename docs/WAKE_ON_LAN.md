# /wake: turning the stream PC on from Discord

`/wake` sends a Wake-on-LAN "magic packet" to the stream PC's network card. The PC has to be
cabled (Wi-Fi wake rarely works) and allowed to wake once:

1. **BIOS / UEFI:** turn on *Wake on LAN* (also called *Power On By PCI-E* or *Resume by LAN*).
   Turn off *ErP / EuP Ready* if your board has it (it cuts power to the network card).
2. **Windows:** Device Manager → Network adapters → your wired card → Properties:
   * *Power Management*: tick *Allow this device to wake the computer* and *Only allow a magic packet*.
   * *Advanced*: *Wake on Magic Packet* = Enabled.
3. Windows *Fast startup* can block wake from "shut down": Control Panel → Power Options → *Choose
   what the power buttons do* → untick *Turn on fast startup* (or use Sleep instead of Shut down).
4. The MAC address: on the stream PC run `ipconfig /all` and copy the wired card's *Physical
   Address* (like `AA-BB-CC-DD-EE-FF`) into `STREAM_PC_MAC`.

The bot container needs `network_mode: host` (already in docker-compose.yml). If your network
blocks 255.255.255.255, set `WOL_BROADCAST` to your network's broadcast address, like `192.168.1.255`.
