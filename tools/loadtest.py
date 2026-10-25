"""Tiny load generator:  python tools/loadtest.py http://127.0.0.1:8000/api/v1/health/ [total=20000] [concurrency=500]"""
import asyncio, sys, time
from collections import Counter

async def main(url, total, conc):
    from urllib.parse import urlsplit
    u = urlsplit(url); host, port = u.hostname, u.port or 80
    req = f"GET {u.path or '/'}{'?' + u.query if u.query else ''} HTTP/1.1\r\nHost: {u.netloc}\r\nConnection: keep-alive\r\n\r\n".encode()
    codes, lat, left = Counter(), [], [total]
    async def worker():
        r, w = await asyncio.open_connection(host, port)
        while left[0] > 0:
            left[0] -= 1
            t = time.perf_counter()
            w.write(req)
            head = await r.readuntil(b"\r\n\r\n")
            n = 0
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"): n = int(line.split(b":")[1])
            await r.readexactly(n)
            lat.append(time.perf_counter() - t); codes[int(head[9:12])] += 1
        w.close()
    t0 = time.perf_counter()
    await asyncio.gather(*[worker() for _ in range(conc)])
    dt = time.perf_counter() - t0; lat.sort()
    print(f"{total} requests in {dt:.1f}s = {total/dt:.0f} rps · p50 {lat[len(lat)//2]*1000:.0f}ms p99 {lat[int(len(lat)*.99)]*1000:.0f}ms · {dict(codes)}")

a = sys.argv
asyncio.run(main(a[1], int(a[2]) if len(a) > 2 else 20000, int(a[3]) if len(a) > 3 else 500))
