import paramiko

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('server.swarupbaral.in', username='titanium', password='titanium@123')

stdin, stdout, _ = client.exec_command("tail -n 80 /home/titanium/dcBot/err.log")
err = stdout.read().decode()
print("=== err.log ===")
print(err if err.strip() else "(empty)")

stdin2, stdout2, _ = client.exec_command("tail -n 80 /home/titanium/dcBot/out.log")
out = stdout2.read().decode()
print("\n=== out.log ===")
print(out if out.strip() else "(empty)")

client.close()
