/** Dashboard realtime voice call — own sidecar session, not the embedded TUI. */

import {
  MAX_CONSULT_OUTPUT_CHARS,
  RealtimeVoiceClient,
  type RealtimeTokenGrant,
  VoiceSupervisorController,
} from "@hermes/shared";
import { Mic, MicOff, Square } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { useI18n } from "@/i18n";
import { GatewayClient } from "@/lib/gatewayClient";
import { cn } from "@/lib/utils";

type CallStatus = "idle" | "connecting" | "listening" | "speaking" | "thinking";

interface TranscriptLine {
  id: number;
  who: "assistant" | "hermes";
  text: string;
}

const MAX_TRANSCRIPT_LINES = 8;

/** Params for the voice card's dedicated sidecar session (not the TUI). */
export function voiceCallSessionCreateParams(
  profile?: string | null,
): Record<string, unknown> {
  return {
    close_on_disconnect: true,
    source: "tool",
    ...(profile ? { profile } : {}),
  };
}

export function VoiceCallCard({ profile }: { profile?: string | null }) {
  const { t } = useI18n();
  const [status, setStatus] = useState<CallStatus>("idle");
  const [muted, setMuted] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [transcript, setTranscript] = useState<TranscriptLine[]>([]);
  const gwRef = useRef<GatewayClient | null>(null);
  const clientRef = useRef<RealtimeVoiceClient | null>(null);
  const controllerRef = useRef<VoiceSupervisorController | null>(null);
  const sessionIdRef = useRef<string | null>(null);
  const busyRef = useRef(false);
  const lineIdRef = useRef(0);

  const pushLine = useCallback((who: TranscriptLine["who"], text: string) => {
    setTranscript((prev) =>
      [...prev, { id: ++lineIdRef.current, who, text }].slice(-MAX_TRANSCRIPT_LINES),
    );
  }, []);

  const stop = useCallback(() => {
    controllerRef.current?.failActiveConsult("Voice session ended.");
    controllerRef.current = null;
    clientRef.current?.close();
    clientRef.current = null;
    gwRef.current?.close();
    gwRef.current = null;
    sessionIdRef.current = null;
    busyRef.current = false;
    setStatus("idle");
    setMuted(false);
  }, []);

  useEffect(() => stop, [stop]);

  const start = useCallback(async () => {
    if (clientRef.current) {
      return;
    }
    setError(null);
    setTranscript([]);
    setStatus("connecting");
    try {
      const gw = new GatewayClient();
      gwRef.current = gw;
      await gw.connect();
      const grant = await gw.request<RealtimeTokenGrant>("voice.realtime_token", {});
      // Invented ids 404 at prompt.submit — the gateway only routes sessions
      // it created, so mint a real sidecar session first.
      const created = await gw.request<{ session_id: string }>(
        "session.create",
        voiceCallSessionCreateParams(profile),
      );
      const sessionId = String(created.session_id || "").trim();
      if (!sessionId) {
        throw new Error("voice session.create returned no session_id");
      }
      sessionIdRef.current = sessionId;

      const client = new RealtimeVoiceClient();
      clientRef.current = client;
      const controller = new VoiceSupervisorController(client, {
        submit: async (task) => {
          const sid = sessionIdRef.current;
          const liveGw = gwRef.current;
          if (!sid || !liveGw) {
            return false;
          }
          busyRef.current = true;
          try {
            await liveGw.request("prompt.submit", {
              session_id: sid,
              text: task,
              ...(profile ? { profile } : {}),
            });
            return true;
          } catch {
            busyRef.current = false;
            return false;
          }
        },
        interrupt: async () => {
          const sid = sessionIdRef.current;
          const liveGw = gwRef.current;
          if (!sid || !liveGw) {
            return;
          }
          await liveGw.request("session.interrupt", { session_id: sid }).catch(() => undefined);
        },
        isBusy: () => busyRef.current,
        isQueueEmpty: () => !busyRef.current,
      });
      controllerRef.current = controller;

      gw.on("message.complete", (ev) => {
        if (ev.session_id !== sessionIdRef.current) {
          return;
        }
        busyRef.current = false;
        const live = controllerRef.current;
        if (!live?.consultActive) {
          return;
        }
        const payload = (ev.payload ?? {}) as { text?: string };
        let output = String(payload.text ?? "").trim() || "Hermes finished with no text output.";
        if (output.length > MAX_CONSULT_OUTPUT_CHARS) {
          output = `${output.slice(0, MAX_CONSULT_OUTPUT_CHARS)}\n[truncated]`;
        }
        pushLine("hermes", output);
        live.onTurnComplete(live.currentTask ?? "", output);
      });

      await client.connect(grant, {
        onFunctionCall: (call) => {
          void controllerRef.current?.onFunctionCall(call.name, call.callId, call.args);
        },
        onAssistantTranscript: (text) => pushLine("assistant", text),
        onUserTranscript: (text) => {
          if (text.trim().toLowerCase().replace(/[.!]/g, "") === "stop") {
            stop();
          }
        },
        onStatus: (clientStatus, detail) => {
          if (!clientRef.current) {
            return;
          }
          if (clientStatus === "speaking") {
            setStatus("speaking");
          } else if (clientStatus === "listening") {
            setStatus(controllerRef.current?.consultActive ? "thinking" : "listening");
          } else if (clientStatus === "error") {
            setError(detail ?? "realtime error");
          } else if (clientStatus === "closed") {
            stop();
          }
        },
      });
      if (clientRef.current !== client) {
        return;
      }
      setStatus("listening");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      stop();
    }
  }, [profile, pushLine, stop]);

  const toggleMute = useCallback(() => {
    setMuted((prev) => {
      const next = !prev;
      clientRef.current?.setMuted(next);
      return next;
    });
  }, []);

  const live = status !== "idle";
  const statusLabel =
    status === "connecting"
      ? t.voiceCall.connecting
      : status === "thinking"
        ? t.voiceCall.working
        : status === "speaking"
          ? t.voiceCall.speaking
          : status === "listening"
            ? t.voiceCall.listening
            : t.voiceCall.title;

  return (
    <div className="px-2 py-2">
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => (live ? stop() : void start())}
          className={cn(
            "flex items-center gap-1.5 rounded border px-2 py-1 text-xs transition-colors",
            live
              ? "border-red-500/50 text-red-400 hover:bg-red-500/10"
              : "border-current/20 hover:bg-current/10",
          )}
          title={live ? t.voiceCall.end : t.voiceCall.start}
        >
          {live ? <Square className="h-3 w-3" /> : <Mic className="h-3 w-3" />}
          <span>{live ? t.voiceCall.end : t.voiceCall.start}</span>
        </button>
        {live && (
          <button
            type="button"
            onClick={toggleMute}
            className="rounded border border-current/20 p-1 hover:bg-current/10"
            title={muted ? t.voiceCall.unmute : t.voiceCall.mute}
          >
            {muted ? <MicOff className="h-3 w-3" /> : <Mic className="h-3 w-3" />}
          </button>
        )}
        <span
          className={cn(
            "ml-auto text-[0.65rem] uppercase tracking-wide opacity-70",
            status === "speaking" && "text-emerald-400",
            status === "thinking" && "text-amber-400",
          )}
        >
          {statusLabel}
        </span>
      </div>
      {error && <div className="mt-1 text-xs text-red-400">{error}</div>}
      {live && transcript.length > 0 && (
        <ul className="mt-2 space-y-1 text-xs opacity-80">
          {transcript.map((line) => (
            <li key={line.id} className="truncate">
              <span className="opacity-60">{line.who === "assistant" ? "🎙" : "⚕"}</span>{" "}
              {line.text}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
