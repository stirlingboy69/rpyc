"""
rpyc plug-in server (threaded or forking)
"""
import sys
import os
import socket
import time
import threading
import select
from rpyc.core import brine, SocketStream, Channel, Connection
from rpyc.utils.logger import Logger
from rpyc.utils.registry import UDPRegistryClient, TCPRegistryClient
from rpyc.utils.authenticators import AuthenticationError


class Server(object):
    def __init__(self, service, hostname = "0.0.0.0", port = 0, backlog = 10, 
    reuse_addr = True, authenticator = None, registrar = None, 
    auto_register = True, protocol_config = {}, logger = None):
        self.active = False
        self._closed = False
        self.service = service
        self.authenticator = authenticator
        self.backlog = backlog
        self.auto_register = auto_register
        self.protocol_config = protocol_config
        self.clients = set()
        if logger is None:
            logger = self._get_logger()
        self.logger = logger
        if registrar is None:
            registrar = UDPRegistryClient(logger = self.logger)
        self.registrar = registrar
        
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if reuse_addr and sys.platform != "win32":
            # warning: reuseaddr is not what you expect on windows!
            self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        
        self.listener.bind((hostname, port))
        self.port = self.listener.getsockname()[1]
    
    def _get_logger(self):
        raise NotImplementedError()
    
    def close(self):
        if self._closed:
            return
        self._closed = True
        self.active = False
        if self.auto_register:
            try:
                self.registrar.unregister(self.port)
            except Exception:
                self.logger.traceback()
        self.listener.close()
        self.logger.info("listener closed")
        for c in set(self.clients):
            try:
                c.shutdown(socket.SHUT_RDWR)
            except:
                pass
            c.close()
        self.clients.clear()
    
    def fileno(self):
        return self.listener.fileno()
    
    def accept(self):
        while True:
            try:
                sock, (h, p) = self.listener.accept()
            except socket.timeout:
                pass
            except socket.error:
                raise EOFError()
            else:
                break
        sock.setblocking(True)
        self.logger.info("accepted %s:%s", h, p)
        if self.authenticator:
            try:
                sock, credentials = self.authenticator(sock)
            except AuthenticationError:
                self.logger.info("%s:%s failed to authenticate, closing socket", h, p)
                sock.close()
                return
            else:
                self.logger.info("%s:%s authenticated successfully", h, p)
        else:
            credentials = None
        self.clients.add(sock)
        self._accept_method(sock, credentials)
    
    def _accept_method(self, sock, credentials):
        raise NotImplementedError
    
    def _serve_client(self, sock, credentials):
        h, p = sock.getpeername()
        self.logger.info("welcome %s:%s", h, p)
        try:
            config = dict(self.protocol_config, credentials = credentials)
            conn = Connection(self.service, Channel(SocketStream(sock)), 
                config = config)
            conn.serve_all()
        finally:
            self.logger.info("goodbye %s:%s", h, p)
            self.clients.discard(sock)
    
    def _bg_register(self):
        interval = self.registrar.REREGISTER_INTERVAL
        self.logger.info("started background auto-register thread "
            "(interval = %s)", interval)
        tnext = 0
        try:
            while self.active:
                t = time.time()
                if t >= tnext: 
                    tnext = t + interval
                    try:
                        self.registrar.register(self.service.get_service_aliases(),
                            self.port)
                    except Exception:
                        self.logger.traceback()
                time.sleep(1)
        finally:
            if not self._closed:
                self.logger.info("background auto-register thread finished")
    
    def start(self):
        """starts the server. use close() to stop"""
        self.listener.listen(self.backlog)
        h, p = self.listener.getsockname()
        self.logger.info("server started on %s:%s", h, p)
        self.active = True
        if self.auto_register:
            t = threading.Thread(target = self._bg_register)
            t.setDaemon(True)
            t.start()
        if sys.platform == "win32":
            # hack so we can receive Ctrl+C on windows
            self.listener.settimeout(1)
        try:
            try:
                while True:
                    self.accept()
            except EOFError:
                pass # server closed by another thread
            except KeyboardInterrupt:
                self.logger.warn("user interrupt!")
        finally:
            self.logger.info("server has terminated")
            self.close()


class ThreadedServer(Server):
    def _get_logger(self):
        return Logger(self.service.get_service_name(), show_tid = True)
    
    def _accept_method(self, sock, credentials):
        t = threading.Thread(target = self._serve_client, args = (sock, credentials))
        t.setDaemon(True)
        t.start()


class ForkingServer(Server):
    def _get_logger(self):
        return Logger(self.service.get_service_name(), show_pid = True)
    
    def _accept_method(self, sock, credentials):
        # we use double-fork to get rid of zombies without having to worry 
        # about EINTR. ugh. forking python processes sucks. 
        pid = os.fork()
        if pid == 0:
            # child
            try:
                self.logger.info("child process created")
                self.listener.close()
                self.clients.clear()
                if os.fork() == 0:
                    # grandchild
                    try:
                        self.logger.info("grandchild starts serving")
                        self._serve_client(sock, credentials)
                    finally:
                        self.logger.info("grandchild terminated")
                        os._exit(0)
            finally:
                self.logger.info("child terminated")
                os._exit(0)
        else:
            # parent
            sock.close()
            os.waitpid(pid, 0)






