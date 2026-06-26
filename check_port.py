import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(2.0)
try:
    s.connect(('127.0.0.1', 8765))
    print("Port 8765 is OPEN")
    s.close()
except Exception as e:
    print(f"Port 8765 is CLOSED: {e}")
