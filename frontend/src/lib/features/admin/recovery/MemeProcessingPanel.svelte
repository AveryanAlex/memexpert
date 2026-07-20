<script lang="ts">
  import type {
    AdminMediaObservationRead,
    AdminMemeProcessingFileRead,
    AdminRecoveryWorkKind
  } from '$lib/api/types';
  import AdminPanel from '$lib/features/admin/AdminPanel.svelte';
  import { formatAdminTimestamp } from '$lib/features/admin/formatTimestamp';
  import { Badge, Notice } from '$lib/ui';
  import RecoveryActionMenu from './RecoveryActionMenu.svelte';
  import { humanizeRecoveryValue } from './view-model';

  let {
    processingFiles,
    requestIds
  }: {
    processingFiles: AdminMemeProcessingFileRead[] | undefined;
    requestIds: Record<string, string>;
  } = $props();

  function originalObservation(file: AdminMemeProcessingFileRead): AdminMediaObservationRead {
    return file.original ?? file.source_observation ?? {
      width: file.width,
      height: file.height,
      file_size_bytes: file.file_size_bytes
    };
  }

  function outputObservation(file: AdminMemeProcessingFileRead): AdminMediaObservationRead | null {
    return file.output ?? file.output_observation ?? null;
  }

  function dimensions(observation: AdminMediaObservationRead | null): string {
    return observation?.width && observation?.height ? `${observation.width}×${observation.height}` : 'Not observed';
  }

  function frameRate(observation: AdminMediaObservationRead | null): string {
    if (observation?.frame_rate === null || observation?.frame_rate === undefined) return 'Not observed';
    if (typeof observation.frame_rate === 'number') return `${observation.frame_rate.toLocaleString('en-US', { maximumFractionDigits: 3 })} FPS`;
    return `${observation.frame_rate} FPS`;
  }

  function duration(observation: AdminMediaObservationRead | null): string {
    return observation?.duration_seconds === null || observation?.duration_seconds === undefined
      ? 'Not observed'
      : `${observation.duration_seconds.toLocaleString('en-US', { maximumFractionDigits: 3 })} s`;
  }

  function bytes(value: number | null | undefined): string {
    if (value === null || value === undefined) return 'Not observed';
    if (value < 1024) return `${value} B`;
    if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KiB`;
    return `${(value / 1024 ** 2).toFixed(1)} MiB`;
  }

  function bitrate(value: number | null | undefined): string {
    return value === null || value === undefined ? 'Not observed' : `${(value / 1_000_000).toFixed(2)} Mbps`;
  }

  function audio(value: boolean | null | undefined): string {
    return value === true ? 'Audio present' : value === false ? 'Silent' : 'Not observed';
  }

  function actionKey(kind: AdminRecoveryWorkKind, workId: string): string {
    return `${kind}:${workId}`;
  }
</script>

<AdminPanel title="Processing" class="my-4">
  <p class="mt-0 text-sm text-muted">Every attached file, its active derivative profile, pipeline truth, and backend-declared replay or repair actions.</p>

  {#if processingFiles === undefined}
    <Notice>Processing detail is not available from this API generation. Moderation data remains usable.</Notice>
  {:else if processingFiles.length === 0}
    <Notice>No attached processing files were returned for this meme.</Notice>
  {:else}
    <div class="grid gap-5">
      {#each processingFiles as file (file.id)}
        {@const original = originalObservation(file)}
        {@const output = outputObservation(file)}
        {@const fileKind = file.work_kind ?? 'pipeline_stage'}
        {@const fileWorkId = file.work_id ?? `${file.id}:transcode`}
        <article class="grid gap-4 rounded-3xl border border-line bg-paper p-4" data-processing-file={file.id}>
          <div class="flex flex-wrap items-start justify-between gap-3">
            <div>
              <div class="flex flex-wrap items-center gap-2">
                <h3 class="m-0 text-xl font-black">File {file.id}</h3>
                {#if file.is_primary}<Badge>Primary</Badge>{/if}
              </div>
              <p class="mb-0 mt-1 text-sm text-muted">{file.mime_type ?? 'Unknown MIME type'} · {humanizeRecoveryValue(file.status)}</p>
            </div>
            {#if file.active_job}<a class="text-sm font-black underline" href={`/admin/recovery/batches/${encodeURIComponent(file.active_job.id)}`}>Active job</a>{/if}
          </div>

          <div class="grid gap-3 lg:grid-cols-2">
            <section class="rounded-2xl border border-line bg-soft p-4">
              <h4 class="m-0 text-base font-black">Original observation</h4>
              <dl class="mb-0 mt-3 grid gap-2 text-sm sm:grid-cols-2">
                <div><dt class="font-extrabold text-muted">Dimensions</dt><dd class="m-0">{dimensions(original)}</dd></div>
                <div><dt class="font-extrabold text-muted">Frame rate</dt><dd class="m-0">{frameRate(original)}</dd></div>
                <div><dt class="font-extrabold text-muted">Duration</dt><dd class="m-0">{duration(original)}</dd></div>
                <div><dt class="font-extrabold text-muted">Byte size</dt><dd class="m-0">{bytes(original.file_size_bytes)}</dd></div>
                <div><dt class="font-extrabold text-muted">Video codec</dt><dd class="m-0">{original.video_codec ?? 'Not observed'}</dd></div>
                <div><dt class="font-extrabold text-muted">Audio</dt><dd class="m-0">{audio(file.source_has_audio)}</dd></div>
              </dl>
            </section>

            <section class="rounded-2xl border border-line bg-soft p-4">
              <h4 class="m-0 text-base font-black">Active web output</h4>
              <dl class="mb-0 mt-3 grid gap-2 text-sm sm:grid-cols-2">
                <div><dt class="font-extrabold text-muted">Profile</dt><dd class="m-0">{file.web_video_profile ?? 'No active profile'}</dd></div>
                <div><dt class="font-extrabold text-muted">Verified</dt><dd class="m-0">{file.web_video_verified_at ? formatAdminTimestamp(file.web_video_verified_at) : 'Not verified'}</dd></div>
                <div><dt class="font-extrabold text-muted">Dimensions</dt><dd class="m-0">{dimensions(output)}</dd></div>
                <div><dt class="font-extrabold text-muted">Frame rate</dt><dd class="m-0">{frameRate(output)}</dd></div>
                <div><dt class="font-extrabold text-muted">Duration</dt><dd class="m-0">{duration(output)}</dd></div>
                <div><dt class="font-extrabold text-muted">Bitrate</dt><dd class="m-0">{bitrate(output?.bitrate_bps)}</dd></div>
                <div><dt class="font-extrabold text-muted">Byte size</dt><dd class="m-0">{bytes(output?.file_size_bytes)}</dd></div>
                <div><dt class="font-extrabold text-muted">Video</dt><dd class="m-0">{[output?.video_codec, output?.pixel_format, output?.video_profile].filter(Boolean).join(' · ') || 'Not observed'}</dd></div>
                <div><dt class="font-extrabold text-muted">Audio</dt><dd class="m-0">{audio(file.web_video_has_audio)}{output?.audio_codec ? ` · ${output.audio_codec}` : ''}</dd></div>
              </dl>
            </section>
          </div>

          {#if file.actions?.length}
            <RecoveryActionMenu
              kind={fileKind}
              workId={fileWorkId}
              version={file.version ?? file.id}
              requestId={requestIds[actionKey(fileKind, fileWorkId)] ?? ''}
              actions={file.actions}
              stage="transcode"
            />
          {/if}

          <section class="grid gap-2">
            <h4 class="m-0 text-lg font-black">Pipeline stages</h4>
            {#if file.stages.length}
              <div class="grid gap-3 lg:grid-cols-2">
                {#each file.stages as stage (`${file.id}:${stage.stage}`)}
                  {@const kind = stage.work_kind ?? 'pipeline_stage'}
                  {@const workId = stage.work_id ?? `${file.id}:${stage.stage}`}
                  <div class="grid gap-3 rounded-2xl border border-line bg-soft p-4">
                    <div class="flex flex-wrap items-start justify-between gap-2">
                      <div><strong>{humanizeRecoveryValue(stage.stage)}</strong><p class="mb-0 mt-1 text-xs text-muted">{humanizeRecoveryValue(stage.status)} · {stage.attempt_count} attempts</p></div>
                      {#if stage.active_job}<a class="text-xs font-black underline" href={`/admin/recovery/batches/${encodeURIComponent(stage.active_job.id)}`}>Active job</a>{/if}
                    </div>
                    {#if stage.safe_error}<p class="m-0 text-xs text-danger">{stage.safe_error}</p>{/if}
                    {#if stage.actions?.length}
                      <RecoveryActionMenu
                        {kind}
                        {workId}
                        version={stage.version}
                        requestId={requestIds[actionKey(kind, workId)] ?? ''}
                        actions={stage.actions}
                        stage={stage.stage}
                        compact
                      />
                    {/if}
                  </div>
                {/each}
              </div>
            {:else}
              <p class="m-0 text-sm text-muted">No stage journal entries are attached.</p>
            {/if}
          </section>
        </article>
      {/each}
    </div>
  {/if}
</AdminPanel>
