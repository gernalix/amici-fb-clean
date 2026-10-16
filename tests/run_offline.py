"""Run synthetic tests with empty private environment and network denied."""
import os, pathlib, socket, subprocess, sys, tempfile, unittest, ast
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'src')]
with tempfile.TemporaryDirectory(prefix='offline-private-') as private:
    keep={k:os.environ[k] for k in ('PATH','LANG','SYSTEMROOT') if k in os.environ}
    os.environ.clear();os.environ.update(keep)
    os.environ.update(HOME=private,XDG_CONFIG_HOME=private+'/config',XDG_DATA_HOME=private+'/data',XDG_STATE_HOME=private+'/state',TELEGRAM_NOTIFY_CONFIG=private+'/missing.env',PYTHONDONTWRITEBYTECODE='1')
    def denied(*a,**kw):raise AssertionError('Offline test attempted network access')
    socket.create_connection=denied;socket.getaddrinfo=denied
    socket.socket.connect=denied;socket.socket.connect_ex=denied
    original=subprocess.Popen
    os.environ['OFFLINE_TEST_HOME']=private
    def guarded(args,*a,**kw):
        command=[os.fspath(x) for x in args] if isinstance(args,(list,tuple)) else []
        exe=pathlib.Path(command[0]).resolve() if command else None
        # Only the existing synthetic launcher fixture, bash syntax validation,
        # and SSH fixture with its own temporary stub may run subprocesses.
        fixture=bool(exe and exe==ROOT/'scripts/run_fedora.sh' and kw.get('env',{}).get('PYTHON_BIN','').startswith(tempfile.gettempdir()+'/'))
        syntax=bool(command and command[0]=='bash' and len(command)==3 and command[1]=='-n' and pathlib.Path(command[2]).is_relative_to(ROOT))
        ssh_fixture=bool(command==['bash',str(ROOT/'tests/test_oracle_ssh.sh')])
        gate=bool(len(command)>4 and command[:2]==['python3',str(ROOT/'scripts/oracle_backup_alert_gate.py')] and command[2]=='--state' and command[3].startswith(tempfile.gettempdir()+'/'))
        sqlfixture=bool(len(command)==3 and pathlib.Path(command[0]).name=='sqlcipher' and command[1]=='-batch' and command[2].startswith(tempfile.gettempdir()+'/') and pathlib.Path(command[2]).name=='state.sqlite')
        if not (fixture or syntax or ssh_fixture or gate or sqlfixture):raise AssertionError('Unmocked operational subprocess denied')
        if kw.get('shell'):raise AssertionError('Shell expansion denied')
        return original(args,*a,**kw)
    subprocess.Popen=guarded
    os.chdir(ROOT)
    # Static checks never execute operator entry points.
    for file in ROOT.rglob('*.py'):
        if '.git' not in file.parts:ast.parse(file.read_text(),filename=str(file))
    for file in ROOT.rglob('*.sh'):
        subprocess.run(['bash','-n',str(file)],check=True)
    suite=unittest.defaultTestLoader.discover(str(ROOT/'tests'))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    if result.wasSuccessful() and (ROOT/'tests/test_oracle_ssh.sh').exists():
        subprocess.run(['bash',str(ROOT/'tests/test_oracle_ssh.sh')],check=True)
    raise SystemExit(0 if result.wasSuccessful() else 1)
