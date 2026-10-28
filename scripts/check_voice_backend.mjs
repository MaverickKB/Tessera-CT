// Explicit live voice acceptance. No microphone, speaker, raw audio or credentials in receipts.
import fs from 'node:fs';
import { randomUUID } from 'node:crypto';

const base = process.env.TESSERA_URL;
const slug = process.env.TESSERA_SESSION;
if (!base || !slug) throw new Error('Set TESSERA_URL and TESSERA_SESSION for an isolated proof session');
const root = `${base.replace(/\/$/, '')}/api/sessions/${encodeURIComponent(slug)}/voice`;
const prompt = process.env.TESSERA_VOICE_PROMPT || 'Call wip_board to read the current work board, then speak the title of one pending card in one short sentence.';
const pcm = process.env.TESSERA_VOICE_PCM ? fs.readFileSync(process.env.TESSERA_VOICE_PCM) : null;
if (pcm && (pcm.length < 4800 || pcm.length % 2)) throw new Error('Synthetic input must be nonempty PCM16 mono at the leased sample rate');
const requireTool = process.env.TESSERA_REQUIRE_TOOL !== '0';
const handoffProof = process.env.TESSERA_HANDOFF_PROOF === '1';
const effort = process.env.TESSERA_VOICE_EFFORT;
if (effort && !['high','none'].includes(effort)) throw new Error('Voice effort must be high or none');
const events = [];
const t0 = Date.now();
let lease;
let socket;
let chain = Promise.resolve();
let toolCalls = 0;
let audioBytes = 0;
let afterToolAudio = 0;
let completed = false;
let taskAccepted = false;
let taskDelivered = false;
let concurrentReply = false;
let askedDuringWork = false;
let providerBusy = false;
let pollTimer;
let toolReturned = false;
let responseText = '';
let inputAt;
let speechStoppedAt;
let firstAudioAt;
let toolResultAt;
let firstPostToolAudioAt;
let fail;
let finish;
const outcome = new Promise((resolve, reject) => { finish = resolve; fail = reject; });
let timer = setTimeout(() => fail(new Error('Voice setup timed out')), 95000);
for (const signal of ['SIGINT', 'SIGTERM']) process.once(signal, () => fail(new Error(`Interrupted by ${signal}`)));
function record(type, extra = {}) {
  const row = {seconds: Math.round((Date.now() - t0) / 10) / 100, type, ...extra};
  let encoded = JSON.stringify(row);
  for (const protocol of lease?.protocols || []) {
    encoded = encoded.replaceAll(protocol, '[redacted]');
    const token = protocol.slice(protocol.indexOf('.') + 1);
    if (token) encoded = encoded.replaceAll(token, '[redacted]');
  }
  const safe = JSON.parse(encoded);
  events.push(safe);
  console.log(encoded);
}
async function post(action, body) {
  const response = await fetch(`${root}/${action}`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body), signal:AbortSignal.timeout(action === 'start' ? 90000 : 20000)});
  const result = await response.json();
  if (!response.ok) throw new Error(`Voice ${action}: HTTP ${response.status}, ${result.error || 'request failed'}`);
  return result;
}
function send(frame) { socket.send(JSON.stringify(frame)); record('sent', {event:frame.type}); }
async function bridge(event) {
  const result = await post('events', {voice_id:lease.voice_id, events:event ? [event] : []});
  record('bridge', {event:event?.type || 'completion_poll', messages:result.messages?.length || 0,
    frames:result.send?.map(frame => frame.type), outputCharacters:result.send?.[0]?.item?.output?.length || 0});
  for (const frame of result.send || []) {
    if (handoffProof && frame.item?.type === 'function_call_output') {
      const output = JSON.parse(frame.item.output);
      if (output.turn_id && !output.replies) {
        taskAccepted = true;
        record('task_accepted', {turnId:output.turn_id, state:output.state});
      }
      if (output.replies && ['done','error','cancelled'].includes(output.state)) {
        if (output.state !== 'done' || !output.replies.length) throw new Error('Background task did not complete successfully');
        taskDelivered = true;
        record('task_delivered', {state:output.state, replies:output.replies});
      }
    }
    send(frame);
    if (frame.item?.type === 'function_call_output') {
      toolReturned = true;
      toolResultAt = Date.now();
      firstPostToolAudioAt = null;
      afterToolAudio = 0;
    }
  }
}
try {
  lease = await post('start', {});
  clearTimeout(timer);
  timer = setTimeout(() => fail(new Error('Voice proof timed out')), handoffProof ? 180000 : 60000);
  record('lease', {tools:lease.session_update.session.tools.map(tool => tool.name), instructionsCharacters:lease.session_update.session.instructions.length});
  if (effort) lease.session_update.session.reasoning = {effort};
  socket = new WebSocket(lease.websocket_url, lease.protocols);
  let ready = false;
  socket.onopen = () => send(lease.session_update);
  socket.onerror = () => fail(new Error('Provider WebSocket error'));
  socket.onclose = event => {
    record('close', {code:event.code, reason:event.reason});
    if (!completed) fail(new Error(`Provider closed before completion (${event.code})`));
  };
  if (handoffProof) {
    pollTimer = setInterval(() => {
      if (taskAccepted && concurrentReply && !providerBusy && !taskDelivered) {
        chain = chain.then(() => bridge(null)).catch(fail);
      }
    }, 1000);
  }
  socket.onmessage = message => {
    const event = JSON.parse(message.data);
    if (event.type === 'response.created') providerBusy = true;
    if (event.type === 'response.output_audio.delta' || event.type === 'response.audio.delta') {
      const bytes = Buffer.from(event.delta || '', 'base64').length;
      audioBytes += bytes;
      if (!firstAudioAt) { firstAudioAt = Date.now(); record('first_audio'); }
      if (toolReturned) {
        afterToolAudio += bytes;
        if (!firstPostToolAudioAt) { firstPostToolAudioAt = Date.now(); record('first_post_tool_audio'); }
      }
      return;
    }
    const detail = {};
    if (event.type === 'session.updated') {
      detail.sessionKeys = Object.keys(event.session || {});
      detail.reasoning = event.session?.reasoning;
      detail.reasoningEffort = event.session?.reasoning_effort;
    }
    if (event.type === 'input_audio_buffer.speech_stopped') speechStoppedAt = Date.now();
    if (event.item_id) detail.itemId = event.item_id;
    if (event.type === 'error') detail.error = event.error;
    if (event.type === 'response.done') {
      detail.status = event.response?.status;
      detail.outputTypes = event.response?.output?.map(item => item.type);
      detail.responseId = event.response?.id;
    }
    if (event.type.endsWith('transcript.done') || event.type === 'conversation.item.input_audio_transcription.completed') detail.transcript = event.transcript;
    record(event.type, detail);
    chain = chain.then(async () => {
      if (event.type === 'error') throw new Error(`Provider error: ${event.error?.code || event.error?.message}`);
      if (event.type === 'session.updated' && !ready) {
        ready = true;
        inputAt = Date.now();
        if (pcm) {
          const chunkBytes = lease.audio_sample_rate * 2 / 10;
          const audio = Buffer.concat([pcm, Buffer.alloc(lease.audio_sample_rate * 2 * 2)]);
          record('audio_input', {bytes:pcm.length, sampleRate:lease.audio_sample_rate});
          // Audio is synthetic PCM16 mono at the leased rate, never microphone capture.
          // Stream outside the event queue so transcription and tools can run during speech.
          (async () => {
            for (let offset = 0; offset < audio.length && !completed; offset += chunkBytes) {
              socket.send(JSON.stringify({type:'input_audio_buffer.append', audio:audio.subarray(offset, offset + chunkBytes).toString('base64')}));
              await new Promise(resolve => setTimeout(resolve, 100));
            }
            record('audio_input_finished');
          })().catch(fail);
        } else {
          const item_id = 'proof-' + randomUUID();
          await bridge({type:'conversation.item.input_audio_transcription.completed',item_id,transcript:prompt});
          send({type:'conversation.item.create',item:{id:item_id,type:'message',role:'user',content:[{type:'input_text',text:prompt}]}});
          send({type:'response.create'});
        }
      }
      if (event.type === 'input_audio_buffer.committed' || event.type === 'conversation.item.input_audio_transcription.completed') {
        await bridge(event);
      }
      if (event.type === 'response.function_call_arguments.done') {
        toolCalls += 1;
        await bridge(event);
      }
      if (event.type === 'response.output_audio_transcript.done' || event.type === 'response.audio_transcript.done') {
        responseText = event.transcript || '';
        await bridge({...event,type:'response.output_audio_transcript.done'});
      }
      if (event.type === 'response.done') {
        await bridge(event);
        providerBusy = false;
        const hasCalls = event.response?.output?.some(item => item.type === 'function_call');
        if (handoffProof && taskAccepted && !taskDelivered && !hasCalls && responseText) {
          if (!askedDuringWork) {
            askedDuringWork = true;
            send({type:'conversation.item.create',item:{type:'message',role:'user',content:[{type:'input_text',text:'While that task runs, confirm briefly that we can continue talking. Do not submit another task.'}]}});
            send({type:'response.create'});
          } else {
            concurrentReply = true;
            record('concurrent_reply', {transcript:responseText});
          }
        }
        if ((!handoffProof || (taskDelivered && concurrentReply)) && (!requireTool || toolReturned) && !hasCalls && responseText && audioBytes > 0 && (!requireTool || afterToolAudio > 0)) {
          completed = true;
          finish();
        }
      }
    }).catch(fail);
  };
  await outcome;
  await chain;
  record('PASS', {taskAccepted, taskDelivered, concurrentReply, toolCalls, audioBytes, afterToolAudio, transcript:responseText,
    inputToFirstAudioMs:firstAudioAt - inputAt,
    endOfSpeechToFirstAudioMs:speechStoppedAt ? firstAudioAt - speechStoppedAt : null,
    toolResultToAudioMs:firstPostToolAudioAt ? firstPostToolAudioAt - toolResultAt : null});
} catch (error) {
  record('FAIL', {message:error.message});
  process.exitCode = 1;
} finally {
  clearTimeout(timer);
  clearInterval(pollTimer);
  socket?.close();
  if (lease?.voice_id) {
    try { await post('stop', {voice_id:lease.voice_id}); record('lease_stopped'); }
    catch { record('lease_stop_failed'); process.exitCode = 1; }
  }
  if (process.env.TESSERA_PROOF_OUT) fs.writeFileSync(process.env.TESSERA_PROOF_OUT, JSON.stringify({completed, toolCalls, audioBytes, afterToolAudio, events}, null, 2) + '\n');
}
