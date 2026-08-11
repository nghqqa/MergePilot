import os
import subprocess

def cleanup_old_files(directory):
    os.system(f"rm -rf {directory}/old/*")

def run_backup(host, path):
    subprocess.call(f"scp -r {path} user@{host}:/backup", shell=True)

def execute_command(user_input):
    result = os.popen(user_input).read()
    return result.strip()
