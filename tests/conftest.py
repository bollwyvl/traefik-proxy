"""General pytest fixtures"""

import asyncio
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
import utils
from certipy import Certipy
from consul.aio import Consul
from jupyterhub.utils import exponential_backoff
from traitlets.log import get_logger

from jupyterhub_traefik_proxy.consul import TraefikConsulProxy
from jupyterhub_traefik_proxy.etcd import TraefikEtcdProxy
from jupyterhub_traefik_proxy.fileprovider import TraefikFileProviderProxy
from jupyterhub_traefik_proxy.traefik_utils import deep_merge

HERE = Path(__file__).parent.resolve()
config_files = os.path.join(HERE, "config_files")


class Config:
    """Namespace for repeated variables.

    N.B. The user names and passwords are also stored in various configuration
    files, saved in ./tests/config_files, both in plain text, and in the case
    of the consul token, base64 encoded (so cannot be grep'ed)."""

    # Force etcdctl to run with the v3 API. This gives us access to various
    # commandss, e.g.  txn
    # Must be passed to the env parameter of any subprocess.Popen call that runs
    # etcdctl
    etcdctl_env = dict(os.environ, ETCDCTL_API="3")

    # Etcd3 auth login credentials
    etcd_password = "secret"
    etcd_user = "root"

    # Consul auth login credentials
    consul_token = "secret"
    consul_port = 8500
    consul_auth_port = 8501

    # Traefik api auth login credentials
    traefik_api_user = "api_admin"
    traefik_api_pass = "admin"

    # The URL that should be proxied to jupyterhub
    # Putting here, can easily change between http and https
    public_url = "https://127.0.0.1:8000"


# Define a "slow" test marker so that we can run the slow tests at the end


def by_slow_marker(item):
    m = item.get_closest_marker("slow")
    if m is None:
        return 0
    else:
        return 1


def pytest_addoption(parser):
    parser.addoption("--slow-last", action="store_true", default=False)


def pytest_collection_modifyitems(items, config):
    if config.getoption("--slow-last"):
        items.sort(key=by_slow_marker)


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: marks tests as slow.")


@pytest.fixture
def dynamic_config_dir():
    # matches traefik.toml
    path = Path("/tmp/jupyterhub-traefik-proxy-test")
    if path.exists():
        shutil.rmtree(path)
    path.mkdir()
    yield path
    shutil.rmtree(path)


@pytest.fixture(scope="session")
def certipy():
    with TemporaryDirectory() as td:
        certipy = Certipy(store_dir=td)
        certipy.create_ca("ca")
        local_names = ["DNS:localhost", "IP:127.0.0.1"]
        # etcd certs from certipy don't work for some reason?
        # I don't understand why, but the originals do
        # certipy.create_signed_pair("etcd", "ca", alt_names=local_names)
        certipy.create_signed_pair("proxy-public", "ca", alt_names=local_names)
        yield certipy


# FIXME (later): certipy-issued certs aren't accepted in etcd
# unclear why, but manually issued certs are okay
@pytest.fixture(scope="session")
def etcd_ssl_key_cert(certipy):
    return (
        str(HERE / "config_files/etcd/etcd.key"),
        str(HERE / "config_files/etcd/etcd.crt"),
    )


@pytest.fixture(scope="session")
def etcd_client_ca(certipy):
    return str(HERE / "config_files/etcd/ca.crt")


@pytest.fixture(scope="session")
def proxy_ssl_key_cert(certipy):
    record = certipy.store.get_record("proxy-public")
    return record['files']['key'], record['files']['cert']


@pytest.fixture(scope="session")
def client_ca(certipy):
    record = certipy.store.get_record("proxy-public")
    from jupyterhub.utils import make_ssl_context
    from tornado.httpclient import AsyncHTTPClient

    ssl_context = make_ssl_context(
        keyfile=record["files"]["key"],
        certfile=record["files"]["cert"],
        cafile=record["files"]["ca"],
        # record["proxy-public"][self.internal_ssl_key,
        # self.internal_ssl_cert,
        # cafile=self.internal_ssl_ca,
    )
    AsyncHTTPClient.configure(None, defaults={"ssl_options": ssl_context})

    print(record)
    return record['files']['ca']


@pytest.fixture
def proxy_args(proxy_ssl_key_cert):
    ssl_key, ssl_cert = proxy_ssl_key_cert
    return dict(
        public_url=Config.public_url,
        traefik_api_password=Config.traefik_api_pass,
        traefik_api_username=Config.traefik_api_user,
        traefik_log_level="DEBUG",
        ssl_key=ssl_key,
        ssl_cert=ssl_cert,
    )


@pytest.fixture
async def no_auth_consul_proxy(launch_consul, proxy_args):
    """
    Fixture returning a configured TraefikConsulProxy.
    Consul acl disabled.
    """
    proxy = TraefikConsulProxy(
        consul_url=f"http://127.0.0.1:{Config.consul_port}",
        should_start=True,
        **proxy_args,
    )
    await proxy.start()
    yield proxy
    await proxy.stop()


@pytest.fixture
async def auth_consul_proxy(launch_consul_auth, proxy_args):
    """
    Fixture returning a configured TraefikConsulProxy.
    Consul acl enabled.
    """
    proxy = TraefikConsulProxy(
        consul_url=f"http://127.0.0.1:{Config.consul_auth_port}",
        consul_password=Config.consul_token,
        should_start=True,
        **proxy_args,
    )
    await proxy.start()
    yield proxy
    await proxy.stop()


@pytest.fixture
async def no_auth_etcd_proxy(launch_etcd, proxy_args):
    """
    Fixture returning a configured TraefikEtcdProxy.
    No etcd authentication.
    """
    proxy = _make_etcd_proxy(auth=False, **proxy_args)
    await proxy.start()
    yield proxy
    await proxy.stop()


@pytest.fixture
async def auth_etcd_proxy(launch_etcd_auth, etcd_client_ca, proxy_args):
    """
    Fixture returning a configured TraefikEtcdProxy
    Etcd has credentials set up
    """
    proxy = _make_etcd_proxy(auth=True, client_ca=etcd_client_ca, **proxy_args)
    await proxy.start()
    yield proxy
    await proxy.stop()


def _make_etcd_proxy(*, auth=False, client_ca=None, **extra_kwargs):
    kwargs = {}
    if auth:
        kwargs.update(
            dict(
                etcd_url="https://localhost:2379",
                etcd_username=Config.etcd_user,
                etcd_password=Config.etcd_password,
                etcd_client_kwargs=dict(
                    grpc_options=[
                        ("grpc.ssl_target_name_override", "localhost"),
                        ("grpc.default_authority", "localhost"),
                    ],
                    ca_cert=client_ca,
                ),
            )
        )
        extra_static = kwargs.setdefault("extra_static_config", {})
        etcd_config = {
            "providers": {
                "etcd": {
                    "tls": {
                        "ca": client_ca,
                        "insecureSkipVerify": True,
                    }
                }
            }
        }
        deep_merge(extra_static, etcd_config)

    kwargs.update(extra_kwargs)
    proxy = TraefikEtcdProxy(**kwargs)
    return proxy


@pytest.fixture(autouse=True)
def traitlets_log():
    """Setup traitlets logger at debug-level

    This is the logger used by all Proxy instances (via LoggingConfigurable)
    """
    log = get_logger()
    log.setLevel(logging.DEBUG)


# There must be a way to parameterise this to run on both yaml and toml files?
@pytest.fixture
async def file_proxy_toml(dynamic_config_dir, proxy_args):
    """Fixture returning a configured TraefikFileProviderProxy"""
    dynamic_config_file = str(dynamic_config_dir / "rules.toml")
    static_config_file = "traefik.toml"
    proxy = TraefikFileProviderProxy(
        dynamic_config_file=dynamic_config_file,
        static_config_file=static_config_file,
        should_start=True,
        **proxy_args,
    )
    await proxy.start()
    yield proxy
    await proxy.stop()


def _check_ssl(proxy, client_ca):
    import pprint

    pprint.pprint(proxy.dynamic_config)
    pprint.pprint(proxy.static_config)

    # check ssl
    import socket
    import ssl

    # ssl_dateformat = r'%b %d %H:%M:%S %Y %Z'

    context = ssl.create_default_context(
        purpose=ssl.Purpose.SERVER_AUTH,
        cafile=client_ca,
    )
    context.check_hostname = True
    context.verify_mode = ssl.VerifyMode.CERT_REQUIRED
    context.load_cert_chain(proxy.ssl_cert, proxy.ssl_key)
    from urllib.parse import urlparse

    url = urlparse(Config.public_url)
    cert = ssl.get_server_certificate((url.hostname, url.port))
    from cryptography import x509
    from cryptography.hazmat.backends import default_backend

    certDecoded = x509.load_pem_x509_certificate(str.encode(cert), default_backend())
    print(certDecoded)
    print(certDecoded.issuer)
    print(certDecoded.subject)
    print(certDecoded.not_valid_after)
    print(certDecoded.not_valid_before)

    conn = context.wrap_socket(
        socket.socket(socket.AF_INET),
        server_hostname=url.hostname,
    )
    # 5 second timeout
    conn.settimeout(5.0)
    conn.connect((url.hostname, url.port))
    ssl_info = conn.getpeercert()
    print(ssl_info, type(ssl_info))
    # assert ssl_info == None


@pytest.fixture
async def file_proxy_yaml(
    dynamic_config_dir, proxy_ssl_key_cert, client_ca, proxy_args
):
    ssl_key, ssl_cert = proxy_ssl_key_cert
    dynamic_config_file = str(dynamic_config_dir / "rules.yaml")
    static_config_file = "traefik.yaml"
    proxy = TraefikFileProviderProxy(
        dynamic_config_file=dynamic_config_file,
        static_config_file=static_config_file,
        should_start=True,
        **proxy_args,
    )

    await proxy.start()

    try:
        _check_ssl(proxy, client_ca)
        yield proxy
    finally:
        await proxy.stop()


@pytest.fixture
async def external_file_proxy_yaml(launch_traefik_file, dynamic_config_dir, proxy_args):
    dynamic_config_file = str(dynamic_config_dir / "rules.yaml")
    proxy = TraefikFileProviderProxy(
        dynamic_config_file=dynamic_config_file,
        should_start=False,
        **proxy_args,
    )
    await proxy._start_future
    yield proxy
    os.remove(dynamic_config_file)


@pytest.fixture
async def external_file_proxy_toml(launch_traefik_file, dynamic_config_dir, proxy_args):
    dynamic_config_file = str(dynamic_config_dir / "rules.toml")
    proxy = TraefikFileProviderProxy(
        dynamic_config_file=dynamic_config_file,
        should_start=False,
        **proxy_args,
    )
    yield proxy
    await proxy._start_future
    os.remove(dynamic_config_file)


@pytest.fixture
async def external_consul_proxy(launch_traefik_consul, proxy_args):
    proxy = TraefikConsulProxy(
        consul_url=f"http://127.0.0.1:{Config.consul_port}",
        should_start=False,
        **proxy_args,
    )
    yield proxy


@pytest.fixture
async def auth_external_consul_proxy(launch_traefik_consul_auth, proxy_args):
    proxy = TraefikConsulProxy(
        consul_url=f"http://127.0.0.1:{Config.consul_auth_port}",
        consul_password=Config.consul_token,
        should_start=False,
        **proxy_args,
    )
    yield proxy


@pytest.fixture
async def external_etcd_proxy(launch_traefik_etcd, proxy_args):
    proxy = _make_etcd_proxy(auth=False, should_start=False, **proxy_args)
    yield proxy
    proxy.etcd.close()


@pytest.fixture
async def auth_external_etcd_proxy(launch_traefik_etcd_auth, proxy_args):
    proxy = _make_etcd_proxy(auth=True, should_start=False, **proxy_args)
    yield proxy
    proxy.etcd.close()


@pytest.fixture(
    params=[
        "no_auth_consul_proxy",
        "auth_consul_proxy",
        "no_auth_etcd_proxy",
        "auth_etcd_proxy",
        "file_proxy_toml",
        "file_proxy_yaml",
        "external_consul_proxy",
        "auth_external_consul_proxy",
        "external_etcd_proxy",
        "auth_external_etcd_proxy",
        "external_file_proxy_toml",
        "external_file_proxy_yaml",
    ]
)
def proxy(request, client_ca, proxy_ssl_key_cert):
    """Parameterized fixture to run all the tests with every proxy implementation"""
    proxy = request.getfixturevalue(request.param)
    ssl_key, ssl_cert = proxy_ssl_key_cert
    # wait for public endpoint to be reachable
    asyncio.run(
        exponential_backoff(
            utils.check_host_up_http,
            f"Proxy public url {proxy.public_url} cannot be reached",
            url=proxy.public_url,
        )
    )
    return proxy


#########################################################################
# Fixtures for launching traefik, with each backend and with or without #
# authentication                                                        #
#########################################################################


@pytest.fixture
def launch_traefik_file():
    args = ("--configfile", os.path.join(config_files, "traefik.toml"))
    print(f"\nLAUNCHING TRAEFIK with args: {args}\n")
    proc = _launch_traefik(*args)
    yield proc
    shutdown_traefik(proc)


@pytest.fixture
def launch_traefik_etcd(launch_etcd):
    env = Config.etcdctl_env
    proc = _launch_traefik_cli("--providers.etcd", env=env)
    yield proc
    shutdown_traefik(proc)


@pytest.fixture
def launch_traefik_etcd_auth(launch_etcd_auth, etcd_client_ca):
    extra_args = (
        "--providers.etcd.tls.insecureSkipVerify=true",
        "--providers.etcd.tls.ca=" + etcd_client_ca,
        "--providers.etcd.username=" + Config.etcd_user,
        "--providers.etcd.password=" + Config.etcd_password,
    )
    proc = _launch_traefik_cli(*extra_args, env=Config.etcdctl_env)
    yield proc
    shutdown_traefik(proc)


@pytest.fixture
def launch_traefik_consul(launch_consul):
    proc = _launch_traefik_cli("--providers.consul")
    yield proc
    shutdown_traefik(proc)


@pytest.fixture
def launch_traefik_consul_auth(launch_consul_auth):
    extra_args = (
        f"--providers.consul.endpoints=http://127.0.0.1:{Config.consul_auth_port}",
    )
    traefik_env = os.environ.copy()
    traefik_env.update({"CONSUL_HTTP_TOKEN": Config.consul_token})
    proc = _launch_traefik_cli(*extra_args, env=traefik_env)
    yield proc
    shutdown_traefik(proc)


def _launch_traefik_cli(*extra_args, env=None):
    default_args = (
        "--api",
        "--log.level=debug",
        "--providers.providersThrottleDuration=0s",
        # "--entrypoints.http.address=:8000",
        "--entrypoints.https.address=:8000",
        "--entrypoints.auth_api.address=:8099",
    )
    args = default_args + extra_args
    return _launch_traefik(*args, env=env)


def _launch_traefik(*extra_args, env=None):
    traefik_launch_command = ("traefik",) + extra_args
    print("launching", traefik_launch_command)
    proc = subprocess.Popen(traefik_launch_command, env=env)
    return proc


#########################################################################
# Fixtures for configuring the traefik providers                        #
#########################################################################

# Etcd Launchers and configurers #
##################################


def _config_etcd(*extra_args):
    data_store_cmd = ("etcdctl", "txn", "--debug") + extra_args
    # Load a pre-baked dynamic configuration into the etcd store.
    # This essentially puts authentication on the traefik api handler.
    with open(os.path.join(config_files, "traefik_etcd_txns.txt")) as fd:
        txns = fd.read()
    proc = subprocess.Popen(
        data_store_cmd, stdin=subprocess.PIPE, env=Config.etcdctl_env
    )
    # need two trailing newlines for etcdctl txn to complete
    proc.communicate(txns.encode() + b'\n\n')
    proc.wait()
    assert (
        proc.returncode == 0
    ), f"{data_store_cmd} exited with status {proc.returncode}"


def _enable_auth_in_etcd(*common_args):
    common_args = list(common_args)
    user = Config.etcd_user
    pw = Config.etcd_password
    subprocess.check_call(
        ["etcdctl", "user", "add", f"{user}:{pw}"] + common_args, env=Config.etcdctl_env
    )
    subprocess.check_call(
        ["etcdctl", "user", "grant-role", user, "root"] + common_args,
        env=Config.etcdctl_env,
    )
    assert (
        subprocess.check_output(
            ["etcdctl", "auth", "enable"] + common_args, env=Config.etcdctl_env
        )
        .decode(sys.stdout.encoding)
        .strip()
        == "Authentication Enabled"
    )


@pytest.fixture
async def launch_etcd_auth(etcd_ssl_key_cert, etcd_client_ca):
    key, cert = etcd_ssl_key_cert
    print(f"{key=} {cert=}")
    os.system(f"openssl x509 -nooout -text -in {cert}")
    etcd_proc = subprocess.Popen(
        [
            "etcd",
            "--log-level=debug",
            "--peer-auto-tls",
            f"--peer-trusted-ca-file={etcd_client_ca}",
            f"--cert-file={cert}",
            f"--key-file={key}",
            "--initial-cluster=default=https://localhost:2380",
            "--initial-advertise-peer-urls=https://localhost:2380",
            "--listen-peer-urls=https://localhost:2380",
            "--listen-client-urls=https://localhost:2379",
            "--advertise-client-urls=https://localhost:2379",
            "--log-level=debug",
        ],
    )
    etcdctl_args = [
        "--endpoints=localhost:2379",
        "--user",
        f"{Config.etcd_user}:{Config.etcd_password}",
        f"--cacert={etcd_client_ca}",
        "--insecure-skip-tls-verify=true",
        "--insecure-transport=false",
        "--debug",
    ]
    try:
        await _wait_for_etcd(*etcdctl_args)
        _enable_auth_in_etcd(*etcdctl_args)
        _config_etcd(*etcdctl_args)
        import etcd3

        c = etcd3.client(
            user=Config.etcd_user,
            password=Config.etcd_password,
            host="localhost",
            port=2379,
            grpc_options=[
                ("grpc.ssl_target_name_override", "localhost"),
                ("grpc.default_authority", "localhost"),
            ],
            ca_cert=etcd_client_ca,
        )
        print("get", list(c.get_prefix("/")))
        yield etcd_proc
    finally:
        shutdown_etcd(etcd_proc)


@pytest.fixture
async def launch_etcd():
    with TemporaryDirectory() as etcd_path:
        etcd_proc = subprocess.Popen(
            ["etcd", "--log-level=debug"],
            cwd=etcd_path,
        )
        try:
            await _wait_for_etcd("--debug=true")
            _config_etcd()
            yield etcd_proc
        finally:
            shutdown_etcd(etcd_proc)


async def _wait_for_etcd(*etcd_args):
    """Etcd may not be ready if we jump straight into the tests.
    Make sure it's running before we continue with configuring it or running
    tests against it.

    In production, etcd would already be running, so don't put this in the
    proxy classes.
    """

    def check():
        p = subprocess.run(
            ["etcdctl", "endpoint", "health", *etcd_args],
            env=Config.etcdctl_env,
            check=False,
            capture_output=True,
            text=True,
        )
        sys.stdout.write(p.stdout)
        sys.stderr.write(p.stderr)
        return "is healthy" in p.stdout + p.stderr

    await exponential_backoff(check, "etcd health check", timeout=10)


# Consul Launchers and configurers #
####################################


@pytest.fixture(scope="module")
def launch_consul():
    with TemporaryDirectory() as consul_path:
        print(f"Launching consul in {consul_path}")
        consul_proc = subprocess.Popen(
            [
                "consul",
                "agent",
                "-dev",
                f"-http-port={Config.consul_port}",
            ],
            cwd=consul_path,
        )
        try:
            asyncio.run(
                _wait_for_consul(token=Config.consul_token, port=Config.consul_port)
            )
            _config_consul()
            yield consul_proc
        finally:
            shutdown_consul(consul_proc)


@pytest.fixture(scope="module")
def launch_consul_auth():
    with TemporaryDirectory() as consul_path:
        consul_proc = subprocess.Popen(
            [
                "consul",
                "agent",
                "-dev",
                # the only one we care about
                f"-http-port={Config.consul_auth_port}",
                # the rest of these are to avoid conflicts
                # https://developer.hashicorp.com/consul/docs/install/ports
                "-dns-port=8610",
                "-server-port=8310",
                "-grpc-port=8512",
                "-grpc-tls-port=8513",
                "-serf-lan-port=8311",
                "-serf-wan-port=8312",
                f"-config-file={config_files}/consul_config.json",
                "-bootstrap-expect=1",
            ],
            cwd=consul_path,
        )
        try:
            # asyncio.run instead of await because this fixture's scope
            # is module-scoped, while event_loop is 'function'-scoped
            asyncio.run(
                _wait_for_consul(
                    token=Config.consul_token, port=Config.consul_auth_port
                )
            )

            _config_consul(secret=Config.consul_token, port=Config.consul_auth_port)
            yield consul_proc
        finally:
            shutdown_consul(
                consul_proc, secret=Config.consul_token, port=Config.consul_auth_port
            )


async def _wait_for_consul(token=None, **kwargs):
    """Consul takes ages to shutdown and start. Make sure it's running before
    we continue with configuring it or running tests against it.

    In production, consul would already be running, so don't put this in the
    proxy classes.
    """

    async def _check_consul():
        try:
            cli = Consul(token=token, **kwargs)
            index, data = await cli.kv.get("getting_any_nonexistent_key_will_do")
        except Exception as e:
            print(f"Consul not up: {e}")
            return False

        print("Consul is up!")
        return True

    await exponential_backoff(
        _check_consul,
        "Consul not available",
        timeout=20,
    )


def _config_consul(secret=None, port=8500):
    proc_env = None
    if secret is not None:
        proc_env = os.environ.copy()
        proc_env.update({"CONSUL_HTTP_TOKEN": secret})

    consul_import_cmd = [
        "consul",
        "kv",
        "import",
        f"-http-addr=http://127.0.0.1:{port}",
        f"@{config_files}/traefik_consul_config.json",
    ]

    """
    Try storing the static config to the kv store.
    Stop if the kv store isn't ready in 60s.
    """
    timeout = time.perf_counter() + 60
    while True:
        if time.perf_counter() > timeout:
            raise Exception("KV not ready! 60s timeout expired!")
        try:
            # Put static config from file in kv store.
            proc = subprocess.check_call(consul_import_cmd, env=proc_env)
            break
        except subprocess.CalledProcessError as e:
            print("Error setting up consul")
            time.sleep(3)


#########################################################################
# Teardown functions                                                    #
#########################################################################


def shutdown_consul(consul_proc, secret=None, port=8500):
    terminate_process(consul_proc, timeout=30)


def shutdown_etcd(etcd_proc):
    terminate_process(etcd_proc, timeout=20)

    # Remove the default.etcd folder, so no settings left over
    # from a completed run
    default_etcd = os.path.join(os.getcwd(), "default.etcd")
    if os.path.exists(default_etcd):
        shutil.rmtree(default_etcd)


def shutdown_traefik(traefik_process):
    terminate_process(traefik_process)


def terminate_process(proc, timeout=5):
    proc.terminate()
    try:
        proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
    finally:
        proc.wait()
