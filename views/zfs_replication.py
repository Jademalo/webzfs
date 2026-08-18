"""
ZFS Replication Management Views
Provides web interface for ZFS replication operations using native send/receive and syncoid
"""
from fastapi import APIRouter, Request, Form, Depends, Body
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from typing import Annotated, Optional, Dict
import json
import platform
import threading
from config.templates import templates
from services.zfs_replication import ZFSReplicationService, ReplicationType, CompressionMethod
from services.syncoid import SyncoidService
from services.zfs_dataset import ZFSDatasetService
from services.zfs_snapshot import ZFSSnapshotService
from services.ssh_connection import SSHConnectionService
from services.storage import FileStorageService
from services.job_scheduler import SyncoidJobScheduler
from services.schedule_utils import (
    validate_cron_expression,
    calculate_next_run,
    get_schedule_presets,
    describe_schedule,
    preview_next_runs,
)
from services.utils import get_openzfs_man_page_section_url, get_os_type, get_zfs_version
from auth.dependencies import get_current_user


router = APIRouter(prefix="/zfs/replication", tags=["zfs-replication"], dependencies=[Depends(get_current_user)])
replication_service = ZFSReplicationService()
syncoid_service = SyncoidService()
dataset_service = ZFSDatasetService()
snapshot_service = ZFSSnapshotService()
ssh_service = SSHConnectionService()
storage_service = FileStorageService()
job_scheduler = SyncoidJobScheduler()


@router.get("/", response_class=HTMLResponse)
async def replication_index(request: Request):
    """Display replication management dashboard.

    Scheduled jobs shown here are Syncoid jobs. The former native
    send/receive job scheduler was removed: its job records lived only in
    process memory, so they were lost on restart and never executed on
    their schedule. One-off native replication is still available through
    ZFS Send/Receive, and all scheduling now goes through Syncoid.
    """
    try:
        scheduled_jobs = _annotate_jobs(storage_service.get_syncoid_jobs())

        # Check syncoid status
        syncoid_status = syncoid_service.check_syncoid_status()

        # Get active executions so user can see in-progress replications
        active_executions = replication_service.get_active_executions()
        
        # Detect OS
        system = platform.system()
        
        return templates.TemplateResponse(
            request,
            name="zfs/replication/index.jinja",
            context={
                "scheduled_jobs": scheduled_jobs,
                "syncoid_status": syncoid_status,
                "active_executions": active_executions,
                "system": system,
                "page_title": "ZFS Replication"
            }
        )
    except Exception as e:
        return templates.TemplateResponse(
            request,
            name="zfs/replication/index.jinja",
            context={
                "scheduled_jobs": [],
                "syncoid_status": {'installed': False},
                "active_executions": [],
                "system": platform.system(),
                "error": str(e),
                "page_title": "ZFS Replication"
            }
        )


# The native replication job routes that used to live here were removed
# along with the native job scheduler. Scheduling is handled by the
# Scheduled Syncoid Jobs routes further down this file, and one-off
# native transfers by the Send/Receive routes below. Any bookmarked
# /zfs/replication/jobs/... URL now lands on the redirect below.


@router.get("/jobs/{rest_of_path:path}")
async def native_jobs_redirect(request: Request, rest_of_path: str):
    """Send old native job URLs to the Syncoid scheduled job list."""
    return RedirectResponse(
        url=(
            "/zfs/replication/syncoid?message="
            "Native replication job scheduling was replaced by "
            "scheduled Syncoid jobs"
        ),
        status_code=303,
    )


# Native ZFS Send/Receive Operations

@router.get("/send-receive/form", response_class=HTMLResponse)
async def send_receive_form(request: Request):
    """Display ZFS send/receive form"""
    try:
        datasets = dataset_service.list_datasets()
        snapshots = snapshot_service.list_snapshots()
        ssh_connections = ssh_service.list_connections()
        
        # Build version-aware man page URLs for zfs-send and zfs-receive
        zfs_send_man_url = get_openzfs_man_page_section_url(8, "zfs-send.8")
        zfs_receive_man_url = get_openzfs_man_page_section_url(8, "zfs-receive.8")
        
        return templates.TemplateResponse(
            request,
            name="zfs/replication/send_receive.jinja",
            context={
                "datasets": datasets,
                "snapshots": snapshots,
                "ssh_connections": ssh_connections,
                "compression_methods": [c.value for c in CompressionMethod],
                "zfs_send_man_url": zfs_send_man_url,
                "zfs_receive_man_url": zfs_receive_man_url,
                "page_title": "ZFS Send/Receive"
            }
        )
    except Exception as e:
        return templates.TemplateResponse(
            request,
            name="zfs/replication/send_receive.jinja",
            context={
                "datasets": [],
                "snapshots": [],
                "ssh_connections": [],
                "zfs_send_man_url": None,
                "zfs_receive_man_url": None,
                "error": str(e),
                "page_title": "ZFS Send/Receive"
            }
        )


@router.post("/send-receive/execute", response_class=HTMLResponse)
async def send_receive_execute(
    request: Request,
    source: Annotated[str, Form()],
    target: Annotated[str, Form()],
    replication_type: Annotated[str, Form()],
    incremental: Annotated[bool, Form()] = False,
    recursive: Annotated[bool, Form()] = False,
    raw: Annotated[bool, Form()] = False,
    large_blocks: Annotated[bool, Form()] = False,
    compression: Annotated[str, Form()] = "lz4",
    remote_host: Annotated[str, Form()] = "",
    remote_port: Annotated[int, Form()] = 22,
    ssh_connection_id: Annotated[str, Form()] = ""
):
    """Execute a one-time ZFS send/receive operation.
    
    Launches the replication in a background thread so the user is
    redirected immediately to the history page where they can monitor
    the active transfer instead of waiting for the request to complete.
    """
    try:
        options = {}
        if remote_host:
            options['remote_host'] = remote_host
            options['remote_port'] = remote_port

        # Look up the SSH connection to get the private key path.
        # Without this, the SSH command would not include -i <key>
        # and would fail with "Permission denied".
        if ssh_connection_id:
            connection = ssh_service.get_connection(ssh_connection_id)
            if connection:
                options['ssh_key'] = connection['private_key_path']
                # If remote_host was not set from the hidden field,
                # build it from the connection record
                if not remote_host:
                    options['remote_host'] = f"{connection['username']}@{connection['host']}"
                    options['remote_port'] = connection['port']
        
        rep_type = ReplicationType(replication_type)
        comp_method = CompressionMethod(compression)
        job_name = f"Manual: {source} → {target}"
        
        # Run replication in a background thread so user gets immediate feedback
        thread = threading.Thread(
            target=replication_service.execute_replication,
            kwargs={
                'source': source,
                'target': target,
                'replication_type': rep_type,
                'incremental': incremental,
                'recursive': recursive,
                'raw': raw,
                'large_blocks': large_blocks,
                'compression': comp_method,
                'job_name': job_name,
                **options
            },
            daemon=True
        )
        thread.start()
        
        # Redirect to history page where the active execution will be visible
        return RedirectResponse(
            url=f"/zfs/replication/history?message=Replication started: {source} → {target}",
            status_code=303
        )
    except Exception as e:
        datasets = dataset_service.list_datasets()
        snapshots = snapshot_service.list_snapshots()
        return templates.TemplateResponse(
            request,
            name="zfs/replication/send_receive.jinja",
            context={
                "datasets": datasets,
                "snapshots": snapshots,
                "compression_methods": [c.value for c in CompressionMethod],
                "error": str(e),
                "page_title": "ZFS Send/Receive"
            }
        )


@router.post("/estimate-size", response_class=HTMLResponse)
async def estimate_size(
    request: Request,
    source: Annotated[str, Form()],
    target: Annotated[str, Form()],
    incremental: Annotated[bool, Form()] = True
):
    """Estimate transfer size for replication"""
    try:
        estimate = replication_service.estimate_transfer_size(
            source=source,
            target=target,
            incremental=incremental
        )
        
        return templates.TemplateResponse(
            request,
            name="zfs/replication/estimate_result.jinja",
            context={
                "estimate": estimate,
                "page_title": "Transfer Size Estimate"
            }
        )
    except Exception as e:
        return templates.TemplateResponse(
            request,
            name="partials/error.jinja",
            context={
                "error": str(e),
                "back_url": "/zfs/replication/send-receive/form"
            }
        )


# Syncoid Operations

@router.get("/syncoid", response_class=HTMLResponse)
async def syncoid_index(request: Request):
    """Display syncoid operations dashboard"""
    try:
        syncoid_status = syncoid_service.check_syncoid_status()
        datasets = dataset_service.list_datasets()
        ssh_connections = ssh_service.list_connections()
        scheduled_jobs = _annotate_jobs(storage_service.get_syncoid_jobs())
        
        # Detect OS
        system = platform.system()
        
        return templates.TemplateResponse(
            request,
            name="zfs/replication/syncoid.jinja",
            context={
                "syncoid_status": syncoid_status,
                "datasets": datasets,
                "ssh_connections": ssh_connections,
                "scheduled_jobs": scheduled_jobs,
                "system": system,
                "page_title": "Syncoid Replication"
            }
        )
    except Exception as e:
        return templates.TemplateResponse(
            request,
            name="zfs/replication/syncoid.jinja",
            context={
                "syncoid_status": {'installed': False},
                "datasets": [],
                "ssh_connections": [],
                "scheduled_jobs": [],
                "system": platform.system(),
                "error": str(e),
                "page_title": "Syncoid Replication"
            }
        )


@router.post("/syncoid/execute", response_class=HTMLResponse)
async def syncoid_execute(
    request: Request,
    source: Annotated[str, Form()],
    target: Annotated[str, Form()],
    recursive: Annotated[bool, Form()] = False,
    no_sync_snap: Annotated[bool, Form()] = False,
    compress: Annotated[str, Form()] = "",
    source_bwlimit: Annotated[str, Form()] = "",
    target_bwlimit: Annotated[str, Form()] = "",
    skip_parent: Annotated[bool, Form()] = False,
    create_bookmark: Annotated[bool, Form()] = False,
    force_delete: Annotated[bool, Form()] = False,
    large_blocks: Annotated[bool, Form()] = False,
    source_ssh_connection_id: Annotated[str, Form()] = "",
    target_ssh_connection_id: Annotated[str, Form()] = ""
):
    """Execute syncoid replication.

    Remote endpoints are resolved server side from the selected SSH
    Manager connection IDs. The managed connection record is the single
    source of truth for username, host, port, and private key, so the
    browser cannot override the SSH destination or identity (issue #195).
    """
    try:
        # Syncoid supports one global --sshkey and --sshport per command.
        # Two different remote connections cannot be represented, so
        # reject that topology explicitly instead of silently using one
        # side's key for both.
        if (
            source_ssh_connection_id
            and target_ssh_connection_id
            and source_ssh_connection_id != target_ssh_connection_id
        ):
            raise Exception(
                "Remote-to-remote replication between two different SSH "
                "connections is not supported yet. One side must be Local, "
                "or both sides must use the same SSH connection."
            )

        # Resolve the managed connection (if any) into an SSH profile
        # containing user@host, port, key path, and strict host key options.
        ssh_profile = None
        active_connection_id = source_ssh_connection_id or target_ssh_connection_id
        if active_connection_id:
            ssh_profile = ssh_service.build_syncoid_profile(active_connection_id)
            ssh_service.mark_connection_used(active_connection_id, 'replication')

        source_host = ssh_profile['host_string'] if source_ssh_connection_id else None
        target_host = ssh_profile['host_string'] if target_ssh_connection_id else None

        result = syncoid_service.execute_replication(
            source=source,
            target=target,
            recursive=recursive,
            no_sync_snap=no_sync_snap,
            compress=compress if compress else None,
            source_bwlimit=source_bwlimit if source_bwlimit else None,
            target_bwlimit=target_bwlimit if target_bwlimit else None,
            skip_parent=skip_parent,
            create_bookmark=create_bookmark,
            force_delete=force_delete,
            send_options="L" if large_blocks else None,
            source_host=source_host,
            target_host=target_host,
            ssh_port=ssh_profile['port'] if ssh_profile and ssh_profile['port'] != 22 else None,
            ssh_key=ssh_profile['identity_file'] if ssh_profile else None,
            ssh_options=ssh_profile['ssh_options'] if ssh_profile else None
        )
        
        return templates.TemplateResponse(
            request,
            name="zfs/replication/syncoid_result.jinja",
            context={
                "result": result,
                "page_title": "Syncoid Result"
            }
        )
    except Exception as e:
        syncoid_status = syncoid_service.check_syncoid_status()
        datasets = dataset_service.list_datasets()
        return templates.TemplateResponse(
            request,
            name="zfs/replication/syncoid.jinja",
            context={
                "syncoid_status": syncoid_status,
                "datasets": datasets,
                "error": str(e),
                "page_title": "Syncoid Replication"
            }
        )


@router.post("/syncoid/test-connection", response_class=HTMLResponse)
async def syncoid_test_connection(
    request: Request,
    remote_host: Annotated[str, Form()],
    remote_port: Annotated[int, Form()] = 22,
    dataset: Annotated[str, Form()] = ""
):
    """Test SSH connection to remote host"""
    try:
        result = syncoid_service.test_connection(
            remote_host=remote_host,
            remote_port=remote_port,
            dataset=dataset if dataset else None
        )
        
        if result['status'] == 'success':
            message = f"Connection to {remote_host} successful"
        else:
            message = f"Connection failed: {result['message']}"
        
        return RedirectResponse(
            url=f"/zfs/replication/syncoid?message={message}",
            status_code=303
        )
    except Exception as e:
        return RedirectResponse(
            url=f"/zfs/replication/syncoid?error={str(e)}",
            status_code=303
        )


@router.post("/syncoid/common-snapshots", response_class=HTMLResponse)
async def syncoid_common_snapshots(
    request: Request,
    source: Annotated[str, Form()],
    target: Annotated[str, Form()],
    source_host: Annotated[str, Form()] = "",
    target_host: Annotated[str, Form()] = ""
):
    """Get common snapshots between source and target"""
    try:
        result = syncoid_service.get_common_snapshots(
            source=source,
            target=target,
            source_host=source_host if source_host else None,
            target_host=target_host if target_host else None
        )
        
        return templates.TemplateResponse(
            request,
            name="zfs/replication/common_snapshots.jinja",
            context={
                "result": result,
                "source": source,
                "target": target,
                "page_title": "Common Snapshots"
            }
        )
    except Exception as e:
        return templates.TemplateResponse(
            request,
            name="partials/error.jinja",
            context={
                "error": str(e),
                "back_url": "/zfs/replication/syncoid"
            }
        )


@router.post("/api/test-remote-connection")
async def test_remote_connection(data: Dict = Body(...)):
    """API endpoint to test remote SSH connection and fetch datasets using SSH connection ID"""
    try:
        import subprocess
        
        ssh_connection_id = data.get('ssh_connection_id')
        
        if not ssh_connection_id:
            return JSONResponse({
                "success": False,
                "error": "SSH connection is required"
            })
        
        # Get the SSH connection
        connection = ssh_service.get_connection(ssh_connection_id)
        if not connection:
            return JSONResponse({
                "success": False,
                "error": "SSH connection not found"
            })
        
        # Get list of datasets from remote using key-based authentication
        try:
            # Build the SSH command using the stored key and the
            # WebZFS-owned known_hosts database for strict verification
            ssh_service.ensure_host_key_trusted(connection['host'], connection['port'])
            ssh_cmd = [
                'ssh',
                '-i', connection['private_key_path'],
                '-p', str(connection['port']),
                '-o', 'BatchMode=yes',
            ]
            for ssh_option in ssh_service.get_host_key_options():
                ssh_cmd.extend(['-o', ssh_option])
            ssh_cmd.extend([
                f"{connection['username']}@{connection['host']}",
                'zfs', 'list', '-H', '-o', 'name'
            ])
            
            process = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=30)
            
            if process.returncode == 0:
                datasets = [line.strip() for line in process.stdout.strip().split('\n') if line.strip()]
                
                # Mark connection as used for replication
                ssh_service.mark_connection_used(ssh_connection_id, 'replication')
                
                return JSONResponse({
                    "success": True,
                    "datasets": datasets,
                    "message": f"Connected to {connection['name']} successfully",
                    "connection": {
                        "id": connection['id'],
                        "name": connection['name'],
                        "host": connection['host'],
                        "username": connection['username'],
                        "port": connection['port']
                    }
                })
            else:
                error_msg = process.stderr if process.stderr else "Connection failed"
                return JSONResponse({
                    "success": False,
                    "error": f"Failed to connect: {error_msg}"
                })
        except subprocess.TimeoutExpired:
            return JSONResponse({
                "success": False,
                "error": "Connection timeout (30 seconds)"
            })
        except Exception as e:
            return JSONResponse({
                "success": False,
                "error": f"Failed to test connection: {str(e)}"
            })
            
    except Exception as e:
        return JSONResponse({
            "success": False,
            "error": str(e)
        })


@router.get("/api/ssh-connections")
async def get_ssh_connections():
    """API endpoint to get list of configured SSH connections"""
    try:
        connections = ssh_service.list_connections()
        return JSONResponse({
            "success": True,
            "connections": connections
        })
    except Exception as e:
        return JSONResponse({
            "success": False,
            "error": str(e)
        })


@router.post("/api/get-remote-datasets")
async def get_remote_datasets(data: Dict = Body(...)):
    """API endpoint to get datasets from a remote system via SSH connection"""
    try:
        import subprocess
        
        ssh_connection_id = data.get('ssh_connection_id')
        
        if not ssh_connection_id:
            # Return local datasets
            local_datasets = dataset_service.list_datasets()
            return JSONResponse({
                "success": True,
                "datasets": [ds['name'] for ds in local_datasets],
                "message": "Local datasets loaded",
                "is_local": True
            })
        
        # Get the SSH connection
        connection = ssh_service.get_connection(ssh_connection_id)
        if not connection:
            return JSONResponse({
                "success": False,
                "error": "SSH connection not found"
            })
        
        # Get list of datasets from remote using key-based authentication
        try:
            ssh_service.ensure_host_key_trusted(connection['host'], connection['port'])
            ssh_cmd = [
                'ssh',
                '-i', connection['private_key_path'],
                '-p', str(connection['port']),
                '-o', 'BatchMode=yes',
                '-o', 'ConnectTimeout=10',
            ]
            for ssh_option in ssh_service.get_host_key_options():
                ssh_cmd.extend(['-o', ssh_option])
            ssh_cmd.extend([
                f"{connection['username']}@{connection['host']}",
                'zfs', 'list', '-H', '-o', 'name'
            ])
            
            process = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=30)
            
            if process.returncode == 0:
                datasets = [line.strip() for line in process.stdout.strip().split('\n') if line.strip()]
                
                return JSONResponse({
                    "success": True,
                    "datasets": datasets,
                    "message": f"Loaded {len(datasets)} datasets from {connection['name']}",
                    "is_local": False,
                    "connection_name": connection['name']
                })
            else:
                error_msg = process.stderr if process.stderr else "Connection failed"
                return JSONResponse({
                    "success": False,
                    "error": f"Failed to get datasets: {error_msg}"
                })
        except subprocess.TimeoutExpired:
            return JSONResponse({
                "success": False,
                "error": "Connection timeout (30 seconds)"
            })
        except Exception as e:
            return JSONResponse({
                "success": False,
                "error": f"Failed to fetch datasets: {str(e)}"
            })
            
    except Exception as e:
        return JSONResponse({
            "success": False,
            "error": str(e)
        })


@router.post("/api/check-large-blocks")
async def check_large_blocks(data: Dict = Body(...)):
    """API endpoint that reports whether a dataset needs zfs send -L.

    Checks recordsize (filesystems) and volblocksize (volumes). Values
    above 128 KiB require the large-block send flag (issue #204). The
    replication forms call this when a source dataset is selected so
    the Large blocks checkbox can be pre-checked automatically.

    Accepts an optional ssh_connection_id to check datasets on a
    remote system through the managed SSH connection.
    """
    try:
        import subprocess

        dataset = (data.get('dataset') or '').strip()
        if not dataset:
            return JSONResponse({
                "success": False,
                "error": "Dataset name is required"
            })

        # Snapshots inherit block size from their dataset
        dataset = dataset.split('@')[0]

        ssh_connection_id = data.get('ssh_connection_id')
        if ssh_connection_id:
            connection = ssh_service.get_connection(ssh_connection_id)
            if not connection:
                return JSONResponse({
                    "success": False,
                    "error": "SSH connection not found"
                })
            # Validate the name locally before it is sent to a shell
            dataset_service.validate_dataset_name(dataset)
            ssh_service.ensure_host_key_trusted(connection['host'], connection['port'])
            ssh_cmd = [
                'ssh',
                '-i', connection['private_key_path'],
                '-p', str(connection['port']),
                '-o', 'BatchMode=yes',
                '-o', 'ConnectTimeout=10',
            ]
            for ssh_option in ssh_service.get_host_key_options():
                ssh_cmd.extend(['-o', ssh_option])
            ssh_cmd.extend([
                f"{connection['username']}@{connection['host']}",
                'zfs', 'get', '-H', '-p', '-o', 'property,value',
                'recordsize,volblocksize', dataset
            ])
            process = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=30)
            if process.returncode != 0:
                return JSONResponse({
                    "success": False,
                    "error": process.stderr or "Failed to read block size"
                })
            block_size_bytes = 0
            for line in process.stdout.strip().split('\n'):
                parts = line.split('\t')
                if len(parts) >= 2 and parts[1].isdigit():
                    block_size_bytes = max(block_size_bytes, int(parts[1]))
            return JSONResponse({
                "success": True,
                "dataset": dataset,
                "block_size_bytes": block_size_bytes,
                "large_blocks": block_size_bytes > dataset_service.LARGE_BLOCK_THRESHOLD_BYTES
            })

        status = dataset_service.get_large_block_status(dataset)
        return JSONResponse({
            "success": True,
            **status
        })
    except Exception as e:
        return JSONResponse({
            "success": False,
            "error": str(e)
        })


@router.get("/history", response_class=HTMLResponse)
async def replication_history(request: Request, limit: int = 50, offset: int = 0):
    """Display replication execution history"""
    try:
        history = replication_service.get_replication_history(limit=limit, offset=offset)
        active_executions = replication_service.get_active_executions()
        
        return templates.TemplateResponse(
            request,
            name="zfs/replication/history.jinja",
            context={
                "history": history,
                "active_executions": active_executions,
                "limit": limit,
                "offset": offset,
                "page_title": "Replication History"
            }
        )
    except Exception as e:
        return templates.TemplateResponse(
            request,
            name="zfs/replication/history.jinja",
            context={
                "history": [],
                "active_executions": [],
                "error": str(e),
                "page_title": "Replication History"
            }
        )


@router.get("/history/{execution_id}", response_class=HTMLResponse)
async def execution_detail(request: Request, execution_id: int):
    """Display detailed execution record with progress updates"""
    try:
        execution = replication_service.get_execution_detail(execution_id)
        
        if not execution:
            return templates.TemplateResponse(
                request,
                name="partials/error.jinja",
                context={
                    "error": f"Execution {execution_id} not found",
                    "back_url": "/zfs/replication/history"
                }
            )
        
        return templates.TemplateResponse(
            request,
            name="zfs/replication/execution_detail.jinja",
            context={
                "execution": execution,
                "page_title": f"Execution #{execution_id}"
            }
        )
    except Exception as e:
        return templates.TemplateResponse(
            request,
            name="partials/error.jinja",
            context={
                "error": str(e),
                "back_url": "/zfs/replication/history"
            }
        )


@router.post("/history/{execution_id}/mark-failed", response_class=HTMLResponse)
async def mark_execution_failed(request: Request, execution_id: int):
    """Mark a running execution as failed.

    Used to clean up stale executions where the background thread
    crashed or the application was restarted mid-replication.
    """
    try:
        success = replication_service.storage.mark_execution_failed(
            execution_id=execution_id,
            error_message=(
                "Manually marked as failed by user. The background "
                "replication process was likely no longer running."
            )
        )

        if success:
            return RedirectResponse(
                url=f"/zfs/replication/history/{execution_id}?message=Execution marked as failed",
                status_code=303
            )
        else:
            return RedirectResponse(
                url=f"/zfs/replication/history/{execution_id}?error=Could not mark execution as failed (not in running state)",
                status_code=303
            )
    except Exception as e:
        return RedirectResponse(
            url=f"/zfs/replication/history/{execution_id}?error={str(e)}",
            status_code=303
        )


@router.post("/history/{execution_id}/delete", response_class=HTMLResponse)
async def delete_execution(request: Request, execution_id: int):
    """Delete a replication execution record from history.

    Only completed (success/failure) executions can be deleted.
    Running executions must be marked as failed first.
    """
    try:
        success = replication_service.storage.delete_execution_record(execution_id)

        if success:
            return RedirectResponse(
                url="/zfs/replication/history?message=Execution record deleted",
                status_code=303
            )
        else:
            return RedirectResponse(
                url=f"/zfs/replication/history?error=Could not delete execution (it may still be running)",
                status_code=303
            )
    except Exception as e:
        return RedirectResponse(
            url=f"/zfs/replication/history?error={str(e)}",
            status_code=303
        )


@router.get("/history/{execution_id}/error-log")
async def execution_error_log(request: Request, execution_id: int):
    """Generate a downloadable markdown error log for a failed execution.

    Includes system information (OS, kernel, ZFS version), execution
    details, the full replication command, error messages, and log
    output to assist with diagnosing replication failures.
    """
    from datetime import datetime as dt

    execution = replication_service.get_execution_detail(execution_id)
    if not execution:
        return Response(
            content="Execution not found",
            status_code=404,
            media_type="text/plain"
        )

    os_type = get_os_type()
    kernel_version = platform.release()
    os_version = platform.version()
    machine_arch = platform.machine()
    zfs_version = get_zfs_version() or "Unknown"
    python_version = platform.python_version()
    generated_at = dt.now().isoformat()

    lines = [
        "# ZFS Replication Error Log",
        "",
        f"**Generated**: {generated_at}",
        f"**Execution ID**: {execution.get('id', 'N/A')}",
        "",
        "---",
        "",
        "## System Information",
        "",
        f"| Field | Value |",
        f"|-------|-------|",
        f"| OS | {os_type} |",
        f"| Kernel | {kernel_version} |",
        f"| OS Version | {os_version} |",
        f"| Architecture | {machine_arch} |",
        f"| ZFS Version | {zfs_version} |",
        f"| Python Version | {python_version} |",
        "",
        "---",
        "",
        "## Execution Details",
        "",
        f"| Field | Value |",
        f"|-------|-------|",
        f"| Job Name | {execution.get('job_name', 'N/A')} |",
        f"| Source Dataset | `{execution.get('source_dataset', 'N/A')}` |",
        f"| Target Dataset | `{execution.get('target_dataset', 'N/A')}` |",
        f"| Replication Type | {execution.get('replication_type', 'N/A')} |",
        f"| Snapshot | `{execution.get('snapshot_name', 'N/A')}` |",
        f"| Status | {execution.get('status', 'N/A')} |",
        f"| Started At | {execution.get('started_at', 'N/A')} |",
        f"| Completed At | {execution.get('completed_at', 'N/A')} |",
        f"| Duration (seconds) | {execution.get('duration_seconds', 'N/A')} |",
    ]

    command = execution.get('command')
    if command:
        lines.extend([
            "",
            "---",
            "",
            "## Replication Command",
            "",
            "```",
            command,
            "```",
        ])

    error_message = execution.get('error_message')
    if error_message:
        lines.extend([
            "",
            "---",
            "",
            "## Error Message",
            "",
            "```",
            error_message,
            "```",
        ])

    log_output = execution.get('log_output')
    if log_output:
        lines.extend([
            "",
            "---",
            "",
            "## Log Output",
            "",
            "```",
            log_output,
            "```",
        ])

    lines.extend([
        "",
        "---",
        "",
        "## Additional Notes",
        "",
        "*Add any additional context or steps to reproduce the issue here.*",
        "",
    ])

    content = "\n".join(lines)
    filename = f"replication-failure-{execution_id}.md"

    return Response(
        content=content,
        media_type="text/markdown",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        }
    )


@router.get("/api/executions/{execution_id}/progress")
async def execution_progress(execution_id: int):
    """JSON endpoint polled by the execution detail page.

    Returns the execution status and the latest progress update so
    the Real-Time Progress panel can refresh without a page reload.
    Polling is used instead of Server-Sent Events because it works
    reliably across gunicorn workers, reverse proxies, and HTMX
    boosted navigation.
    """
    try:
        detail = replication_service.get_execution_detail(execution_id)
        if not detail:
            return JSONResponse({
                "success": False,
                "error": "Execution not found"
            }, status_code=404)

        progress_updates = detail.get('progress_updates') or []
        latest_progress = progress_updates[-1] if progress_updates else None

        response = {
            "success": True,
            "execution_id": execution_id,
            "status": detail.get('status'),
            "job_name": detail.get('job_name'),
            "update_count": len(progress_updates),
        }

        if latest_progress:
            response["progress"] = {
                "timestamp": latest_progress.get('timestamp'),
                "percentage": latest_progress.get('percentage_complete', 0) or 0,
                "bytes_transferred": latest_progress.get('bytes_transferred', 0) or 0,
                "transfer_rate": latest_progress.get('transfer_rate') or 'N/A',
                "eta": latest_progress.get('estimated_time_remaining') or 'N/A',
                "status_message": latest_progress.get('status_message') or ''
            }
        else:
            response["progress"] = None

        return JSONResponse(response)
    except Exception as e:
        return JSONResponse({
            "success": False,
            "error": str(e)
        }, status_code=500)


@router.get("/notifications/settings", response_class=HTMLResponse)
async def notification_settings(request: Request):
    """Display email notification settings"""
    try:
        email_service = replication_service.email
        is_configured = email_service.is_configured()
        
        return templates.TemplateResponse(
            request,
            name="zfs/replication/notification_settings.jinja",
            context={
                "is_configured": is_configured,
                "smtp_enabled": email_service.smtp_enabled,
                "smtp_host": email_service.smtp_host,
                "smtp_port": email_service.smtp_port,
                "smtp_from_address": email_service.smtp_from_address,
                "recipients": email_service.notification_recipients,
                "page_title": "Notification Settings"
            }
        )
    except Exception as e:
        return templates.TemplateResponse(
            request,
            name="zfs/replication/notification_settings.jinja",
            context={
                "is_configured": False,
                "error": str(e),
                "page_title": "Notification Settings"
            }
        )


@router.post("/notifications/test", response_class=HTMLResponse)
async def test_notifications(request: Request):
    """Test email notification configuration"""
    try:
        email_service = replication_service.email
        result = email_service.test_configuration()
        
        if result['status'] == 'sent':
            message = "Test email sent successfully!"
        else:
            message = f"Failed to send test email: {result['message']}"
        
        return RedirectResponse(
            url=f"/zfs/replication/notifications/settings?message={message}",
            status_code=303
        )
    except Exception as e:
        return RedirectResponse(
            url=f"/zfs/replication/notifications/settings?error={str(e)}",
            status_code=303
        )


# Scheduled Syncoid Jobs (issue #194)

def _annotate_jobs(jobs):
    """Add display fields (connection name, schedule label, next run) to jobs."""
    connections = {c['id']: c for c in ssh_service.list_connections()}
    for job in jobs:
        connection = connections.get(job.get('ssh_connection_id'))
        job['connection_name'] = connection['name'] if connection else None
        job['connection_missing'] = bool(
            job.get('ssh_connection_id') and not connection
        )
        job['schedule_label'] = describe_schedule(job.get('schedule', ''))
        if not job.get('next_run'):
            job['next_run'] = calculate_next_run(job.get('schedule', ''))
    return jobs


def _parse_job_form_error(request: Request, error: str, job=None):
    """Render the job form again with an error message."""
    return templates.TemplateResponse(
        request,
        name="zfs/replication/syncoid_job_form.jinja",
        context={
            "job": job,
            "ssh_connections": ssh_service.list_connections(),
            "schedule_presets": get_schedule_presets(),
            "error": error,
            "page_title": "Scheduled Syncoid Job"
        }
    )


@router.get("/syncoid/jobs/create/form", response_class=HTMLResponse)
async def syncoid_job_create_form(request: Request):
    """Display the create form for a scheduled Syncoid job"""
    return templates.TemplateResponse(
        request,
        name="zfs/replication/syncoid_job_form.jinja",
        context={
            "job": None,
            "ssh_connections": ssh_service.list_connections(),
            "schedule_presets": get_schedule_presets(),
            "page_title": "Create Scheduled Syncoid Job"
        }
    )


@router.get("/syncoid/jobs/{job_id}/edit", response_class=HTMLResponse)
async def syncoid_job_edit_form(request: Request, job_id: int):
    """Display the edit form for a scheduled Syncoid job"""
    job = storage_service.get_syncoid_job(job_id)
    if not job:
        return RedirectResponse(
            url="/zfs/replication/syncoid?error=Scheduled job not found",
            status_code=303
        )
    return templates.TemplateResponse(
        request,
        name="zfs/replication/syncoid_job_form.jinja",
        context={
            "job": job,
            "ssh_connections": ssh_service.list_connections(),
            "schedule_presets": get_schedule_presets(),
            "page_title": f"Edit Scheduled Syncoid Job: {job['name']}"
        }
    )


@router.post("/syncoid/jobs/save", response_class=HTMLResponse)
async def syncoid_job_save(
    request: Request,
    name: Annotated[str, Form()],
    source_dataset: Annotated[str, Form()],
    target_dataset: Annotated[str, Form()],
    schedule: Annotated[str, Form()],
    replication_type: Annotated[str, Form()] = "local",
    ssh_connection_id: Annotated[str, Form()] = "",
    job_id: Annotated[str, Form()] = "",
    enabled: Annotated[bool, Form()] = False,
    recursive: Annotated[bool, Form()] = False,
    no_sync_snap: Annotated[bool, Form()] = False,
    compress: Annotated[str, Form()] = "",
    source_bwlimit: Annotated[str, Form()] = "",
    target_bwlimit: Annotated[str, Form()] = "",
    skip_parent: Annotated[bool, Form()] = False,
    create_bookmark: Annotated[bool, Form()] = False,
    force_delete: Annotated[bool, Form()] = False,
    large_blocks: Annotated[bool, Form()] = False,
):
    """Create or update a scheduled Syncoid job and register it with
    the OS scheduler (systemd timer on Linux, root crontab on BSD)."""
    form_job = {
        "id": int(job_id) if job_id else None,
        "name": name,
        "source_dataset": source_dataset,
        "target_dataset": target_dataset,
        "schedule": schedule,
        "replication_type": replication_type,
        "ssh_connection_id": ssh_connection_id or None,
        "enabled": enabled,
        "recursive": recursive,
        "no_sync_snap": no_sync_snap,
        "compress": compress or None,
        "source_bwlimit": source_bwlimit or None,
        "target_bwlimit": target_bwlimit or None,
        "skip_parent": skip_parent,
        "create_bookmark": create_bookmark,
        "force_delete": force_delete,
        "large_blocks": large_blocks,
    }

    try:
        schedule = schedule.strip()
        is_valid, schedule_error = validate_cron_expression(schedule)
        if not is_valid:
            return _parse_job_form_error(request, schedule_error, form_job)

        if replication_type in ("push", "pull") and not ssh_connection_id:
            return _parse_job_form_error(
                request,
                "A SSH connection is required for push and pull jobs.",
                form_job,
            )
        if replication_type == "local":
            ssh_connection_id = ""

        # Verify the referenced connection exists and is active
        if ssh_connection_id:
            connection = ssh_service.get_connection(ssh_connection_id)
            if not connection:
                return _parse_job_form_error(
                    request, "Selected SSH connection was not found.", form_job
                )

        if job_id:
            saved_id = int(job_id)
            updated = storage_service.update_syncoid_job(
                job_id=saved_id,
                name=name,
                source_dataset=source_dataset,
                target_dataset=target_dataset,
                schedule=schedule,
                enabled=enabled,
                recursive=recursive,
                no_sync_snap=no_sync_snap,
                compress=compress or "",
                source_bwlimit=source_bwlimit or "",
                target_bwlimit=target_bwlimit or "",
                skip_parent=skip_parent,
                create_bookmark=create_bookmark,
                force_delete=force_delete,
                large_blocks=large_blocks,
                ssh_connection_id=ssh_connection_id,
                replication_type=replication_type,
            )
            if not updated:
                return _parse_job_form_error(
                    request, "Scheduled job not found.", form_job
                )
            action = "updated"
        else:
            saved_id = storage_service.create_syncoid_job(
                name=name,
                source_dataset=source_dataset,
                target_dataset=target_dataset,
                schedule=schedule,
                enabled=enabled,
                recursive=recursive,
                no_sync_snap=no_sync_snap,
                compress=compress or None,
                source_bwlimit=source_bwlimit or None,
                target_bwlimit=target_bwlimit or None,
                skip_parent=skip_parent,
                create_bookmark=create_bookmark,
                force_delete=force_delete,
                large_blocks=large_blocks,
                ssh_connection_id=ssh_connection_id or None,
                replication_type=replication_type,
            )
            action = "created"

        storage_service.update_syncoid_job_status(
            job_id=saved_id,
            next_run=calculate_next_run(schedule) or "",
        )

        # Register with the OS scheduler
        saved_job = storage_service.get_syncoid_job(saved_id)
        job_scheduler.register_job(saved_job)

        return RedirectResponse(
            url=f"/zfs/replication/syncoid?message=Scheduled job '{name}' {action}",
            status_code=303
        )
    except Exception as e:
        return _parse_job_form_error(request, str(e), form_job)


@router.post("/syncoid/jobs/{job_id}/enable", response_class=HTMLResponse)
async def syncoid_job_enable(request: Request, job_id: int):
    """Enable a scheduled Syncoid job and register its OS schedule"""
    try:
        storage_service.update_syncoid_job(job_id=job_id, enabled=True)
        job = storage_service.get_syncoid_job(job_id)
        if job:
            storage_service.update_syncoid_job_status(
                job_id=job_id,
                next_run=calculate_next_run(job.get('schedule', '')) or "",
            )
            job_scheduler.register_job(job)
        return RedirectResponse(
            url="/zfs/replication/syncoid?message=Scheduled job enabled",
            status_code=303
        )
    except Exception as e:
        return RedirectResponse(
            url=f"/zfs/replication/syncoid?error={str(e)}",
            status_code=303
        )


@router.post("/syncoid/jobs/{job_id}/disable", response_class=HTMLResponse)
async def syncoid_job_disable(request: Request, job_id: int):
    """Disable a scheduled Syncoid job and remove its OS schedule"""
    try:
        storage_service.update_syncoid_job(job_id=job_id, enabled=False)
        job_scheduler.unregister_job(job_id)
        return RedirectResponse(
            url="/zfs/replication/syncoid?message=Scheduled job disabled",
            status_code=303
        )
    except Exception as e:
        return RedirectResponse(
            url=f"/zfs/replication/syncoid?error={str(e)}",
            status_code=303
        )


@router.post("/syncoid/jobs/{job_id}/delete", response_class=HTMLResponse)
async def syncoid_job_delete(request: Request, job_id: int):
    """Delete a scheduled Syncoid job and remove its OS schedule"""
    try:
        job_scheduler.unregister_job(job_id)
        storage_service.delete_syncoid_job(job_id)
        return RedirectResponse(
            url="/zfs/replication/syncoid?message=Scheduled job deleted",
            status_code=303
        )
    except Exception as e:
        return RedirectResponse(
            url=f"/zfs/replication/syncoid?error={str(e)}",
            status_code=303
        )


@router.post("/syncoid/jobs/{job_id}/run", response_class=HTMLResponse)
async def syncoid_job_run_now(request: Request, job_id: int):
    """Run a scheduled Syncoid job immediately in a background thread.

    Uses the same runner code path as the OS scheduler, so results
    appear in the replication history and the job's last run status.
    """
    try:
        job = storage_service.get_syncoid_job(job_id)
        if not job:
            return RedirectResponse(
                url="/zfs/replication/syncoid?error=Scheduled job not found",
                status_code=303
            )

        from services.syncoid_runner import run_syncoid_job
        thread = threading.Thread(
            target=run_syncoid_job,
            kwargs={"job_id": job_id, "trigger": "manual"},
            daemon=True
        )
        thread.start()

        return RedirectResponse(
            url=(
                "/zfs/replication/history?message="
                f"Job '{job['name']}' started; progress appears here"
            ),
            status_code=303
        )
    except Exception as e:
        return RedirectResponse(
            url=f"/zfs/replication/syncoid?error={str(e)}",
            status_code=303
        )


@router.post("/syncoid/jobs/validate-schedule")
async def syncoid_job_validate_schedule(data: Dict = Body(...)):
    """Validate a cron expression and preview upcoming run times."""
    expression = (data.get("schedule") or "").strip()
    is_valid, schedule_error = validate_cron_expression(expression)
    if not is_valid:
        return JSONResponse({"valid": False, "error": schedule_error})
    return JSONResponse({
        "valid": True,
        "next_runs": preview_next_runs(expression, count=5),
    })
