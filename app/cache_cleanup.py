"""Allowlisted cleanup of regenerable application files. Never touch user data."""
from dataclasses import dataclass
from pathlib import Path
import ctypes
import gc
import os
import re
import stat
import time

DAY=86400
LABELS={'bytecode':'可重建的 Python 缓存','diagnostic':'过期临时截图 · 保留最近 3 天',
        'temporary':'遗留保存临时文件 · 保留最近 24 小时'}


@dataclass(frozen=True)
class Candidate:
    path: str
    kind: str
    size: int
    mtime_ns: int
    inode: int
    device: int


def safe_stat(path,root):
    """Reject files outside this installation and all symlink/reparse ancestors."""
    path=Path(path);root=Path(root).resolve()
    try:
        path.absolute().relative_to(root)
        path.resolve().relative_to(root)
        cur=path
        while cur!=root:
            st=cur.lstat()
            if stat.S_ISLNK(st.st_mode) or getattr(st,'st_file_attributes',0)&0x400:
                return None
            cur=cur.parent
        st=path.stat()
        return st if stat.S_ISREG(st.st_mode) else None
    except (OSError,ValueError):
        return None


def classify(path,root,now):
    root=Path(root).resolve();path=Path(path)
    st=safe_stat(path,root)
    if st is None:return None
    rel=path.absolute().relative_to(root)
    parts=rel.parts
    if len(parts)>=3 and parts[0] in ('app','lib') and path.parent.name=='__pycache__':
        match=re.fullmatch(r'(.+)\.cpython-\d+(?:\.opt-\d+)?\.pyc',path.name)
        if match and safe_stat(path.parent.parent/(match[1]+'.py'),root):
            return 'bytecode',st
    age=now-st.st_mtime
    if len(parts)==2 and parts[0]=='app' and re.fullmatch(r'config\.json\.tmp\.\d+',path.name):
        if age>=DAY:return 'temporary',st
    if parts[0]=='logs' and age>=3*DAY:
        if len(parts)==3 and parts[1]=='probe' and path.suffix.lower() in ('.png','.txt'):
            return 'diagnostic',st
        if len(parts)==2 and (re.fullmatch(r'm\d+-(marker|cleared|at|filter|final)\.png',path.name)
                             or path.name in ('qq-panel-unsandboxed.png','qq-panel-ocr.txt','panel-now.txt')):
            return 'diagnostic',st
    return None


def _walk(base,root):
    if not base.is_dir():return
    for folder,dirs,files in os.walk(base,followlinks=False):
        kept=[]
        for name in dirs:
            if name in ('assets','model_v2','model','sprites'):continue
            p=Path(folder)/name
            try:
                st=p.lstat()
                if not (stat.S_ISLNK(st.st_mode) or getattr(st,'st_file_attributes',0)&0x400):
                    p.resolve().relative_to(root);kept.append(name)
            except (OSError,ValueError):pass
        dirs[:]=kept
        for name in files:yield Path(folder)/name


def scan(root,now=None):
    root=Path(root).resolve();now=time.time() if now is None else now
    candidates=[];warnings=[]
    # Do not enumerate runtime, data, assets, backups or arbitrary temporary folders.
    paths=[]
    for name in ('app','lib'):
        base=root/name
        try:
            info=base.lstat()
        except FileNotFoundError:continue
        except OSError as exc:
            warnings.append(name+' 目录无法读取：'+str(exc));continue
        if base.is_symlink() or getattr(info,'st_file_attributes',0)&0x400:
            warnings.append(name+' 目录为链接，已跳过');continue
        paths.extend(_walk(base,root) or [])
    logs=root/'logs'
    if logs.is_dir():
        try:
            if not (logs.is_symlink() or getattr(logs.lstat(),'st_file_attributes',0)&0x400):
                paths.extend(p for p in logs.iterdir() if p.is_file())
                probe=logs/'probe'
                if probe.is_dir() and not (probe.is_symlink() or getattr(probe.lstat(),'st_file_attributes',0)&0x400):
                    paths.extend(probe.iterdir())
        except OSError as exc:warnings.append('部分诊断目录无法读取：'+str(exc))
    for p in paths:
        found=classify(p,root,now)
        if found:
            kind,st=found
            candidates.append(Candidate(str(p),kind,st.st_size,st.st_mtime_ns,st.st_ino,st.st_dev))
    candidates.sort(key=lambda c:(c.kind,c.path))
    return {'files':candidates,'bytes':sum(c.size for c in candidates),'warnings':warnings}


def clean(root,candidates,now=None):
    root=Path(root).resolve();now=time.time() if now is None else now
    count=freed=0;skipped=[]
    for item in candidates:
        found=classify(Path(item.path),root,now)
        if not found:
            skipped.append((item.path,'已变化或不再符合清理范围'));continue
        kind,st=found
        if (kind,st.st_size,st.st_mtime_ns,st.st_ino,st.st_dev)!=(item.kind,item.size,item.mtime_ns,item.inode,item.device):
            skipped.append((item.path,'扫描后已更新，已保留'));continue
        try:
            Path(item.path).unlink()
            count+=1;freed+=item.size
        except OSError as exc:skipped.append((item.path,str(exc)))
    return {'count':count,'bytes':freed,'skipped':skipped}


def memory_usage():
    """Current process working set, measured without trimming or touching other processes."""
    if os.name!='nt':return None
    from ctypes import wintypes
    class Counters(ctypes.Structure):
        _fields_=[('cb',wintypes.DWORD),('faults',wintypes.DWORD)]+[(name,ctypes.c_size_t) for name in
            ('peak','working','peak_paged','paged','peak_nonpaged','nonpaged','pagefile','peak_pagefile','private')]
    try:
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.GetCurrentProcess.restype=wintypes.HANDLE
        psapi=ctypes.WinDLL('psapi',use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes=[wintypes.HANDLE,ctypes.POINTER(Counters),wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype=wintypes.BOOL
        data=Counters();data.cb=ctypes.sizeof(data)
        if psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(),ctypes.byref(data),data.cb):return data.working
    except (OSError,AttributeError):pass
    return None


def collect_memory():
    before=memory_usage();objects=gc.collect();after=memory_usage()
    return {'objects':objects,'before':before,'after':after}


def size_text(value):
    if value is None:return '暂不可用'
    if value<1024:return f'{value} B'
    if value<1024**2:return f'{value/1024:.1f} KB'
    return f'{value/1024**2:.1f} MB'
