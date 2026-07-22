import paramiko

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('server.swarupbaral.in', username='titanium', password='titanium@123')

# Read latest out.log and err.log
stdin, stdout, _ = client.exec_command("tail -n 60 /home/titanium/dcBot/out.log")
out_log = stdout.read().decode()
print("=== LATEST OUT.LOG ===")
print(out_log if out_log.strip() else "(empty)")

stdin2, stdout2, _ = client.exec_command("tail -n 60 /home/titanium/dcBot/err.log")
err_log = stdout2.read().decode()
print("\n=== LATEST ERR.LOG ===")
print(err_log if err_log.strip() else "(empty)")

stdin3, stdout3, _ = client.exec_command("tail -n 60 /home/titanium/dcBot/music_bot.log 2>/dev/null || echo ''")
music_log = stdout3.read().decode()
print("\n=== LATEST MUSIC_BOT.LOG ===")
print(music_log if music_log.strip() else "(empty)")

# Delete / clear old log files
cmd_clean = "> /home/titanium/dcBot/out.log; > /home/titanium/dcBot/err.log; > /home/titanium/dcBot/music_bot.log"
client.exec_command(cmd_clean)
print("\nOld log files truncated/cleared on VPS.")

client.close()
