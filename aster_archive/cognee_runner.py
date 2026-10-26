"""Private isolated Cognee child. Only bridge.py invokes this file directly.

No archive DB or OAuth credentials enter this interpreter. Model preparation is
an explicit download-only operation; processing forbids outbound Python sockets.
"""
from __future__ import annotations

import asyncio
import ctypes
import hashlib
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import resource
import signal
import socket
import sys
import time

COGNEE_VERSION='1.6.3'
GLINER_REPO='fastino/gliner2.5-base-v1'
GLINER_REVISION='54785d51df8d86a0d8f2eb212fec15871aad2c4d'
EMBEDDING_MODEL='BAAI/bge-small-en-v1.5'
EMBEDDING_REPO='qdrant/bge-small-en-v1.5-onnx-q'
EMBEDDING_REVISION='aa8f8b060edb00e03bfdd08813a2949946c8ba55'


def _parent_fence():
    expected=int(os.environ['ASTER_COGNEE_PARENT_PID'])
    if sys.platform.startswith('linux'):
        libc=ctypes.CDLL(None,use_errno=True)
        if libc.prctl(1,signal.SIGTERM,0,0,0)!=0:
            raise RuntimeError('parent_fence_failed')
    if os.getppid()!=expected:
        raise RuntimeError('parent_changed')


def _block_network(event,args):
    if event=='socket.connect' and args[0].family in (socket.AF_INET,socket.AF_INET6):
        raise PermissionError('offline_processing_only')
    if event=='socket.getaddrinfo':
        raise PermissionError('offline_processing_only')


def _hash(path):
    digest=hashlib.sha256()
    with path.open('rb') as handle:
        for data in iter(lambda:handle.read(4*1024**2),b''):
            digest.update(data)
    return digest.hexdigest()


def prepare(model_root,expected_manifest):
    from huggingface_hub import snapshot_download
    specs=((GLINER_REPO,GLINER_REVISION,'gliner'),(EMBEDDING_REPO,EMBEDDING_REVISION,'embedding'))
    paths={}
    files={}
    for repo,revision,name in specs:
        path=Path(snapshot_download(repo_id=repo,revision=revision,
            cache_dir=str(model_root / 'downloads'),allow_patterns=['*.json','*.txt','*.safetensors','*.onnx'],
            max_workers=2))
        paths[name]=str(path.relative_to(model_root))
        for file in sorted(path.rglob('*')):
            if file.is_file():
                files[str(file.relative_to(model_root))]=_hash(file)
    manifest={'cognee_version':COGNEE_VERSION,'gliner_model':GLINER_REPO,
        'gliner_revision':GLINER_REVISION,'embedding_model':EMBEDDING_MODEL,
        'embedding_repository':EMBEDDING_REPO,'embedding_revision':EMBEDDING_REVISION,
        'paths':paths,'files':files}
    if manifest!=expected_manifest:
        raise ValueError('downloaded_model_manifest_mismatch')
    target=model_root / 'manifest.json'
    temp=model_root / 'manifest.json.tmp'
    temp.write_text(json.dumps(manifest,sort_keys=True));temp.chmod(0o600)
    os.replace(temp,target)
    return {'status':'complete','models':2,'model_files':len(files),
            'manifest_sha256':_hash(target),'model_language':'English embedding pilot'}


def verify_models(model_root,expected_manifest):
    manifest=json.loads((model_root/'manifest.json').read_text())
    if manifest!=expected_manifest:
        raise ValueError('model_manifest_mismatch')
    root=model_root.resolve()
    for relative,digest in manifest['files'].items():
        path=(root/relative).resolve(strict=True)
        # HuggingFace snapshots use local symlinks into their own blob cache.
        if not path.is_relative_to(root) or _hash(path)!=digest:
            raise ValueError('model_file_mismatch')
    return manifest


def _pipeline_complete(result):
    rows=list(result.values()) if isinstance(result,dict) else [result]
    if not rows or any(getattr(row,'status',None) not in ('PipelineRunCompleted','PipelineRunAlreadyCompleted') for row in rows):
        raise RuntimeError('cognee_pipeline_incomplete')


async def derive(payload,model_root):
    started=time.monotonic()
    manifest=verify_models(model_root,payload['verified_model_manifest'])
    receipt_root=Path.cwd() / 'receipts'
    receipt_root.mkdir(exist_ok=True,mode=0o700)
    receipt_file=receipt_root / (payload['passage_id']+'.json')
    source_sha256=hashlib.sha256(payload['text'].encode()).hexdigest()
    manifest_sha256=_hash(model_root/'manifest.json')
    if receipt_file.is_file() and receipt_file.stat().st_size<128_000:
        previous=json.loads(receipt_file.read_text())
        if (previous.get('source_sha256')==source_sha256 and previous.get('manifest_sha256')==manifest_sha256
                and previous.get('status')=='complete'):
            return previous | {'reused_completed_receipt':True}
    import torch
    torch.set_num_threads(int(os.environ['ASTER_COGNEE_CPU_THREADS']))
    torch.set_num_interop_threads(1)
    import cognee
    from fastembed import TextEmbedding
    # Cognee's Fastembed adapter instantiates the supported TextEmbedding API
    # without exposing its path/thread kwargs. Pin those kwargs in this private
    # process before any engine is constructed; never edit installed packages.
    from importlib import import_module
    embedding_module=import_module('cognee.infrastructure.databases.vector.embeddings.FastembedEmbeddingEngine')
    from functools import partial
    embedding_module.TextEmbedding=partial(TextEmbedding,
        specific_model_path=str(model_root/manifest['paths']['embedding']),
        local_files_only=True,threads=int(os.environ['ASTER_COGNEE_CPU_THREADS']),cuda=False)
    # Preserve the registry method used by Cognee's first-load notice.
    embedding_module.TextEmbedding.list_supported_models=TextEmbedding.list_supported_models
    logging.disable(logging.CRITICAL)
    try:
        from loguru import logger
        logger.remove()
    except ImportError:
        pass
    from cognee.modules.cognify.config import get_cognify_config,resolve_extractor
    from cognee.infrastructure.llm.config import get_llm_config
    from cognee.infrastructure.databases.vector.embeddings.get_embedding_engine import get_embedding_engine
    if get_llm_config().llm_api_key or resolve_extractor('gliner_demo',get_cognify_config())!='gliner_demo':
        raise RuntimeError('local_only_configuration_failed')
    engine=get_embedding_engine()
    if type(engine).__name__!='FastembedEmbeddingEngine' or engine.model!=EMBEDDING_MODEL:
        raise RuntimeError('local_embedding_configuration_failed')
    from cognee.tasks.graph.gliner_demo.tasks import get_gliner_demo_tasks,GlinerRunStats
    stats=GlinerRunStats()
    dataset='archive_passage_'+payload['passage_id']
    # Prefix makes this unambiguously text input even when the historical
    # passage starts with a URL, filename or other loader-sensitive string.
    document='Archived source passage.\n\n'+payload['text']
    _pipeline_complete(await cognee.add(document,dataset_name=dataset,data_per_batch=1,
                                        run_in_background=False,incremental_loading=True))
    tasks=await get_gliner_demo_tasks(
        entity_types=['person','organization','project','technology','place','task'],
        relation_types=['uses','depends_on','part_of','works_for','located_in','created_by','born_in'],
        model_name=str(model_root/manifest['paths']['gliner']),
        gliner_batch_size=1,chunks_per_batch=1,chunk_size=256,
        check_contradictions=False,embed_triplets=False,stats=stats)
    _pipeline_complete(await cognee.run_custom_pipeline(tasks=tasks,dataset=dataset,
        pipeline_name='cognify_pipeline',data_per_batch=1,incremental_loading=True,
        run_in_background=False,data_cache=True))
    # Explicit raw summaries retrieval does not generate an answer or invoke
    # other tools; datasets constrain the query to this one source passage.
    found=await cognee.recall(payload['text'],query_type=cognee.SearchType.SUMMARIES,
        datasets=[dataset],top_k=16,auto_route=False,feedback_influence=0,
        tool_connections=[])
    summaries=[]
    for item in found:
        text=getattr(item,'text',None)
        if getattr(item,'source',None)=='graph' and isinstance(text,str) and text and text not in summaries:
            summaries.append(text)
    if stats.chunks>16 or len(summaries)>16 or any(len(value)>8000 for value in summaries):
        raise RuntimeError('summary_budget_exceeded')
    await cognee.wait_for_background_tasks()
    result={'status':'complete','summaries':summaries,'graph_nodes':stats.nodes,
            'graph_edges':stats.kept_edges,'chunks':stats.chunks,
            'candidate_edges':stats.candidate_edges,'kept_edges':stats.kept_edges,
            'manifest_sha256':manifest_sha256,'source_sha256':source_sha256,
            'elapsed_seconds':round(time.monotonic()-started,3)}
    temporary=receipt_file.with_suffix('.tmp')
    temporary.write_text(json.dumps(result,ensure_ascii=False));temporary.chmod(0o600)
    os.replace(temporary,receipt_file)
    return result


def main():
    _parent_fence()
    os.umask(0o077)
    if importlib.metadata.version('cognee')!=COGNEE_VERSION:
        raise ValueError('cognee_version_mismatch')
    for key in ('LLM_API_KEY','OPENAI_API_KEY','ANTHROPIC_API_KEY','COGNEE_API_KEY'):
        if os.environ.get(key):
            raise ValueError('unexpected_provider_credential')
    if os.environ.get('ASTER_COGNEE_OFFLINE')=='1':
        sys.addaudithook(_block_network)
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    # Virtual address space includes mmap stores and libraries. RSS is monitored
    # by the parent; deployment also supplies a six-GiB cgroup memory limit.
    memory=int(os.environ['ASTER_COGNEE_MEMORY_BYTES'])
    resource.setrlimit(resource.RLIMIT_AS,(memory*2,memory*2))
    resource.setrlimit(resource.RLIMIT_CPU,(3600,3600))
    request,response=map(Path,sys.argv[1:3])
    if request.stat().st_size>32_000:
        raise ValueError('request_budget_exceeded')
    payload=json.loads(request.read_text())
    model_root=Path(os.environ['ASTER_COGNEE_MODEL_ROOT']).resolve()
    started=time.monotonic()
    if payload['operation']=='prepare':
        result=prepare(model_root,payload['verified_model_manifest'])
    elif payload['operation']=='derive':
        import re
        if not re.fullmatch('[a-f0-9]{64}',payload['passage_id']) or payload['role'] not in ('user','assistant'):
            raise ValueError('invalid_passage_metadata')
        if not isinstance(payload['text'],str) or len(payload['text'])>2000:
            raise ValueError('passage_budget_exceeded')
        result=asyncio.run(derive(payload,model_root))
    else:
        raise ValueError('invalid_operation')
    result.setdefault('elapsed_seconds',round(time.monotonic()-started,3))
    result['invocation_elapsed_seconds']=round(time.monotonic()-started,3)
    response.write_text(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    try:
        main()
    except BaseException as error:
        try:
            Path(sys.argv[2]).write_text(json.dumps({'status':'failed','error_code':type(error).__name__}))
        except BaseException:
            pass
        # Do not emit source strings, library exceptions or paths. The parent
        # persists a stable error code and retries only within its fixed budget.
        sys.exit(21)
