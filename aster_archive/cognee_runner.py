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


class RuntimeInitializationFailure(RuntimeError):
    """Only fixed configuration codes leave runtime initialization."""
    def __init__(self,code):
        self.code=code
        super().__init__(code)


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


async def _initialize_runtime(runtime,model_root):
    manifest=runtime['manifest']
    import torch
    device=os.environ.get('ASTER_COGNEE_DEVICE','cpu')
    if device not in ('cpu','cuda'):
        raise ValueError('invalid_execution_device')
    if device=='cuda':
        if not torch.cuda.is_available():
            raise RuntimeError('requested_cuda_unavailable')
        torch.cuda.set_per_process_memory_fraction(float(os.environ['ASTER_COGNEE_GPU_MEMORY_FRACTION']))
        torch.backends.cuda.matmul.allow_tf32=False
        torch.backends.cudnn.allow_tf32=False
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
    from cognee.tasks.graph.gliner_demo.extractor import get_extractor
    extractor=await get_extractor(str(model_root/manifest['paths']['gliner']))
    if not callable(getattr(extractor,'to',None)) or not callable(getattr(extractor,'parameters',None)):
        raise RuntimeError('unsupported_gliner_device_interface')
    extractor.to(device)
    extractor.eval()
    devices={parameter.device.type for parameter in extractor.parameters()}
    if devices!={device}:
        raise RuntimeError('gliner_execution_device_mismatch')
    dtypes={str(parameter.dtype) for parameter in extractor.parameters() if parameter.is_floating_point()}
    if dtypes!={'torch.float32'}:
        raise RuntimeError('gliner_precision_mismatch')
    runtime.update(initialized=True,cognee=cognee,provenance={
        'execution_device':device,'torch_version':str(torch.__version__),
        'cuda_runtime':str(torch.version.cuda) if torch.version.cuda else None,
        'model_dtype':'float32','cpu_threads':torch.get_num_threads(),
        'inference_batch_size':int(os.environ.get('ASTER_COGNEE_INFERENCE_BATCH_SIZE','1')),
        'model_loads_in_process':1,'model_manifest_verifications_in_process':1,
        'runner_pid':os.getpid()})
    if device=='cuda':
        runtime['provenance']['cuda_device_name']=torch.cuda.get_device_name(0)


async def derive(payload,model_root,*,runtime=None):
    started=time.monotonic()
    runtime=runtime if runtime is not None else {}
    if 'manifest' not in runtime:
        try:
            runtime['manifest']=verify_models(model_root,payload['verified_model_manifest'])
            runtime['manifest_sha256']=_hash(model_root/'manifest.json')
        except Exception:
            raise RuntimeInitializationFailure('MODEL_VERIFICATION_FAILED') from None
    manifest=runtime['manifest']
    receipt_root=Path.cwd() / 'receipts'
    receipt_root.mkdir(exist_ok=True,mode=0o700)
    receipt_file=receipt_root / (payload['passage_id']+'.json')
    source_sha256=hashlib.sha256(payload['text'].encode()).hexdigest()
    manifest_sha256=runtime['manifest_sha256']
    if receipt_file.is_file() and receipt_file.stat().st_size<128_000:
        previous=json.loads(receipt_file.read_text())
        if (previous.get('source_sha256')==source_sha256 and previous.get('manifest_sha256')==manifest_sha256
                and previous.get('status')=='complete'):
            return previous | {'reused_completed_receipt':True}
    if not runtime.get('initialized'):
        try:
            await _initialize_runtime(runtime,model_root)
        except Exception as error:
            code=_safe_error_code(error)
            if code not in _RUNTIME_CONFIGURATION_CODES:
                code='RUNTIME_INITIALIZATION_FAILED'
            raise RuntimeInitializationFailure(code) from None
    cognee=runtime['cognee']
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
        gliner_batch_size=int(os.environ.get('ASTER_COGNEE_INFERENCE_BATCH_SIZE','1')),
        chunks_per_batch=int(os.environ.get('ASTER_COGNEE_INFERENCE_BATCH_SIZE','1')),chunk_size=256,
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
            'elapsed_seconds':round(time.monotonic()-started,3),
            'runtime':runtime['provenance']}
    temporary=receipt_file.with_suffix('.tmp')
    with temporary.open('w') as handle:
        json.dump(result,handle,ensure_ascii=False)
        handle.flush();os.fsync(handle.fileno())
    temporary.chmod(0o600)
    os.replace(temporary,receipt_file)
    directory_fd=os.open(receipt_root,os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return result



def _validate_passage(payload):
    import re
    if not re.fullmatch('[a-f0-9]{64}',payload.get('passage_id','')) or payload.get('role') not in ('user','assistant'):
        raise ValueError('invalid_passage_metadata')
    if not isinstance(payload.get('text'),str) or len(payload['text'])>2000:
        raise ValueError('passage_budget_exceeded')


def _atomic_response(path,payload):
    data=json.dumps(payload,ensure_ascii=False)
    if len(data.encode())>128_000:
        raise ValueError('response_budget_exceeded')
    temporary=path.with_suffix('.tmp')
    with temporary.open('w') as handle:
        handle.write(data);handle.flush();os.fsync(handle.fileno())
    temporary.chmod(0o600);os.replace(temporary,path)


_RUNTIME_CONFIGURATION_CODES=frozenset({
    'CUDA_UNAVAILABLE','GLINER_DEVICE_INTERFACE_UNSUPPORTED',
    'GLINER_DEVICE_MISMATCH','GLINER_PRECISION_MISMATCH',
    'RUNTIME_INITIALIZATION_FAILED','MODEL_VERIFICATION_FAILED',
    'LOCAL_ONLY_CONFIGURATION_FAILED','LOCAL_EMBEDDING_CONFIGURATION_FAILED',
    'INVALID_EXECUTION_DEVICE','CHILD_LAUNCH_FAILED',
})


def _safe_error_code(error):
    # Only these literal configuration messages may leave the child. Arbitrary
    # library errors can contain historical content and are never propagated.
    known={'requested_cuda_unavailable':'CUDA_UNAVAILABLE',
           'unsupported_gliner_device_interface':'GLINER_DEVICE_INTERFACE_UNSUPPORTED',
           'gliner_execution_device_mismatch':'GLINER_DEVICE_MISMATCH',
           'gliner_precision_mismatch':'GLINER_PRECISION_MISMATCH',
           'local_only_configuration_failed':'LOCAL_ONLY_CONFIGURATION_FAILED',
           'local_embedding_configuration_failed':'LOCAL_EMBEDDING_CONFIGURATION_FAILED',
           'invalid_execution_device':'INVALID_EXECUTION_DEVICE'}
    if isinstance(error,RuntimeInitializationFailure):
        return error.code if error.code in _RUNTIME_CONFIGURATION_CODES else 'RUNTIME_INITIALIZATION_FAILED'
    return known.get(str(error),type(error).__name__)


async def serve(payload,model_root,work):
    import re
    allowed=payload.get('passage_ids')
    if (not isinstance(allowed,list) or not 1<=len(allowed)<=20
            or len(set(allowed))!=len(allowed)
            or any(not isinstance(item,str) or not re.fullmatch('[a-f0-9]{64}',item) for item in allowed)):
        raise ValueError('invalid_session_passages')
    runtime={}
    used=set()
    for index in range(len(allowed)):
        request=work/f'request-{index}.json'
        response=work/f'response-{index}.json'
        while not request.is_file():
            await asyncio.sleep(0.05)
        try:
            if request.stat().st_size>32_000:
                raise ValueError('request_budget_exceeded')
            item=json.loads(request.read_text())
            _validate_passage(item)
            if item['passage_id'] not in allowed or item['passage_id'] in used:
                raise ValueError('session_passage_mismatch')
            used.add(item['passage_id'])
            item['verified_model_manifest']=payload['verified_model_manifest']
            result=await derive(item,model_root,runtime=runtime)
            _atomic_response(response,result)
        except Exception as error:
            _atomic_response(response,{'status':'failed','error_code':_safe_error_code(error)})
            return {'status':'failed','completed_requests':index}
    return {'status':'complete','completed_requests':len(allowed)}


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
    # by the parent and the deployment cgroup. CUDA reserves a much larger
    # virtual range than resident RAM, so only CPU children use RLIMIT_AS.
    memory=int(os.environ['ASTER_COGNEE_MEMORY_BYTES'])
    if os.environ.get('ASTER_COGNEE_DEVICE','cpu')!='cuda':
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
        _validate_passage(payload)
        result=asyncio.run(derive(payload,model_root))
    elif payload['operation']=='serve':
        result=asyncio.run(serve(payload,model_root,request.parent))
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
            Path(sys.argv[2]).write_text(json.dumps({'status':'failed','error_code':_safe_error_code(error)}))
        except BaseException:
            pass
        # Do not emit source strings, library exceptions or paths. The parent
        # persists a stable error code and retries only within its fixed budget.
        sys.exit(21)
