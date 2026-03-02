import subprocess

service = """[Unit]
Description=Eng Test Controller
Requires=docker.service
After=docker.service network-online.target

[Service]
WorkingDirectory=/home/nvidia/eng_test
ExecStart=/usr/libexec/docker/cli-plugins/docker-compose up --build
ExecStop=/usr/libexec/docker/cli-plugins/docker-compose down
Restart=always
RestartSec=5
User=nvidia

[Install]
WantedBy=multi-user.target
"""

with open('/etc/systemd/system/eng_test.service', 'w') as f:
    f.write(service)

print("Service file written.")
subprocess.run(['systemctl', 'daemon-reload'])
subprocess.run(['systemctl', 'enable', 'eng_test'])
subprocess.run(['systemctl', 'restart', 'eng_test'])
subprocess.run(['systemctl', 'status', 'eng_test'])
