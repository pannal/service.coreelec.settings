# SPDX-License-Identifier: GPL-2.0
# Copyright (C) 2020-present Team LibreELEC (https://libreelec.tv)
# Copyright (C) 2020-present Team CoreELEC (https://coreelec.org)

import asyncio
import threading

import dbussy
import ravel

import log


BUS_NAME = ''
INTERFACE_AGENT = ''
PATH_AGENT = ''


class Agent(object):

    def __init__(self, bus_name, path_agent):
        self.bus_name = bus_name
        self.path_agent = path_agent
        if self.bus_name in list_names(timeout=10):
            self.register_agent()
        self.watch_name()

    @log.log_function()
    def watch_name(self):
        BUS.listen_signal(
            interface=dbussy.DBUS.SERVICE_DBUS,
            fallback=True,
            func=self.on_name_owner_changed,
            path='/',
            name='NameOwnerChanged')

    @ravel.signal(name='NameOwnerChanged', in_signature='sss', arg_keys=('name', 'old_owner', 'new_owner'))
    async def on_name_owner_changed(self, name, old_owner, new_owner):
        if name == self.bus_name and new_owner != '':
            self.register_agent()

    @log.log_function()
    def register_agent(self):
        BUS.request_name(
            self.bus_name, flags=dbussy.DBUS.NAME_FLAG_DO_NOT_QUEUE)
        BUS.register(
            path=self.path_agent, interface=self, fallback=True)
        self.manager_register_agent()

    @log.log_function()
    def unregister_agent(self):
        self.manager_unregister_agent()
        BUS.unregister(path=self.path_agent)

    def manager_register_agent(self):
        pass

    def manager_unregister_agent(self):
        pass


class Bool(int):

    def __new__(cls, value):
        return int.__new__(cls, bool(value))

    def __str__(self):
        return '1' if self == True else '0'


class LoopThread(threading.Thread):

    def __init__(self, loop):
        super().__init__()
        self.loop = loop
        self.is_stopped = False

    @log.log_function()
    async def wait(self):
        while not self.is_stopped:
            await asyncio.sleep(1)

    @log.log_function()
    def run(self):
        try:
            self.loop.run_until_complete(self.wait())
        except (Exception, asyncio.CancelledError):
            pass

    @log.log_function()
    def stop(self):
        self.is_stopped = True
        if self.is_alive():
            self.join(timeout=2)


def list_names(timeout=None):
    """Get list of registered D-Bus names.

    When timeout is given, runs the call in a thread and returns an
    empty list if it doesn't complete in time (prevents blocking for
    25s when dbus is shutting down).
    """
    if timeout is None:
        return BUS[dbussy.DBUS.SERVICE_DBUS]['/'].get_interface(dbussy.DBUS.INTERFACE_DBUS).ListNames()[0]
    import threading
    result = [None]
    def _call():
        try:
            result[0] = BUS[dbussy.DBUS.SERVICE_DBUS]['/'].get_interface(dbussy.DBUS.INTERFACE_DBUS).ListNames()[0]
        except Exception:
            pass
    t = threading.Thread(target=_call)
    t.daemon = True
    t.start()
    t.join(timeout=timeout)
    return result[0] if result[0] is not None else []


def convert_from_dbussy(data):
    if isinstance(data, bool):
        return Bool(data)
    if isinstance(data, dict):
        return {key: convert_from_dbussy(data[key]) for key in data.keys()}
    if isinstance(data, list):
        return [convert_from_dbussy(item) for item in data]
    if isinstance(data, tuple) and isinstance(data[0], dbussy.DBUS.Signature):
        return convert_from_dbussy(data[1])
    return data


def call_method(bus_name, path, interface, method_name, *args, **kwargs):
    timeout = kwargs.pop('timeout', 10)
    result_holder = [None]
    def _call():
        try:
            iface = BUS[bus_name][path].get_interface(interface)
            method = getattr(iface, method_name)
            result = method(*args, **kwargs)
            first = next(iter(result or []), None)
            result_holder[0] = convert_from_dbussy(first)
        except Exception:
            pass
    import threading
    t = threading.Thread(target=_call)
    t.daemon = True
    t.start()
    t.join(timeout=timeout)
    return result_holder[0]


async def call_async_method(bus_name, path, interface, method_name, *args, **kwargs):
    interface = await BUS[bus_name][path].get_async_interface(interface)
    method = getattr(interface, method_name)
    result = await method(*args, **kwargs)
    first = next(iter(result or []), None)
    return convert_from_dbussy(first)


def run_method(bus_name, path, interface, method_name, *args, **kwargs):
    future = asyncio.run_coroutine_threadsafe(call_async_method(
        bus_name, path, interface, method_name, *args, **kwargs), LOOP)
    return future.result(timeout=30)


try:
    LOOP = asyncio.get_running_loop()
except RuntimeError:
    LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(LOOP)

BUS = ravel.system_bus()
BUS.attach_asyncio(LOOP)
LOOP_THREAD = LoopThread(LOOP)
LOOP_THREAD.daemon = True  # Ensure Python exits even if thread is blocked
