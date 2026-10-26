"""Real loopback lost-response tests: a timeout must never replay a command."""
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import threading
import time
import unittest
from urllib.parse import urlsplit

from selenium.webdriver.chrome.options import Options
from driver_lifecycle import create_owned_chrome, DriverConnectionLost


class TransportFenceTests(unittest.TestCase):
    def test_a_live_driver_with_a_lost_submit_reply_has_exactly_one_browser_action(self):
        accepted=[];forwards=[];release=threading.Event()
        class Page(BaseHTTPRequestHandler):
            def do_GET(self):
                body=b'<button onclick="fetch(\'/accepted\',{method:\'POST\'})">Fixture submit</button>'
                self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            def do_POST(self):
                accepted.append(True);self.send_response(200);self.send_header('Content-Length','0');self.end_headers()
            def log_message(self,*_):pass
        with ExitStack() as stack:
            page=ThreadingHTTPServer(('127.0.0.1',0),Page)
            threading.Thread(target=page.serve_forever,daemon=True).start()
            stack.callback(page.server_close);stack.callback(page.shutdown)
            options=Options();options.add_argument('--headless=new')
            driver=create_owned_chrome(options)
            actual=driver.command_executor.client_config.remote_server_addr
            guard=driver.command_executor.execute.__self__
            def dispose():
                release.set()
                driver.command_executor.client_config.remote_server_addr=actual
                driver.command_executor.client_config.timeout=5
                driver.command_executor.execute=guard._execute
                driver.quit()
            driver.get('http://127.0.0.1:'+str(page.server_port))
            upstream=urlsplit(actual)
            class Proxy(BaseHTTPRequestHandler):
                def do_POST(self):
                    body=self.rfile.read(int(self.headers.get('Content-Length','0')))
                    forwards.append(self.path)
                    connection=http.client.HTTPConnection(upstream.hostname,upstream.port,timeout=5)
                    connection.request('POST',self.path,body,{'Content-Type':'application/json'})
                    response=connection.getresponse();data=response.read();status=response.status;connection.close()
                    release.wait(3)
                    try:
                        self.send_response(status);self.send_header('Content-Type','application/json')
                        self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
                    except (ConnectionError,OSError):pass
                def log_message(self,*_):pass
            proxy=ThreadingHTTPServer(('127.0.0.1',0),Proxy)
            threading.Thread(target=proxy.serve_forever,daemon=True).start()
            stack.callback(proxy.server_close);stack.callback(proxy.shutdown);stack.callback(dispose)
            driver.command_executor.client_config.remote_server_addr='http://127.0.0.1:'+str(proxy.server_port)
            driver.command_executor.client_config.timeout=.25
            with self.assertRaises(DriverConnectionLost) as caught:
                driver.execute_script("document.querySelector('button').click();return true")
            self.assertTrue(caught.exception.driver_alive)
            self.assertIn('Timeout',caught.exception.transport_error)
            self.assertEqual(caught.exception.command,'w3cExecuteScript')
            with self.assertRaises(DriverConnectionLost) as again:
                driver.execute_script("document.querySelector('button').click()")
            self.assertIs(caught.exception,again.exception)
            until=time.monotonic()+2
            while not accepted and time.monotonic()<until:time.sleep(.01)
            self.assertEqual(accepted,[True])
            self.assertEqual(len(forwards),1)


if __name__=='__main__':
    unittest.main()
