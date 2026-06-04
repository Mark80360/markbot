import { useCallback, useEffect, useRef, useState } from "react";
import type { Message, SessionInfo, ToolCallInfo } from "@/types/chat";

function generateId(): string {
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
}

function authHeaders(): Record<string, string> {
  const w = window as any;
  const token: string = w.__MARKBOT_SESSION_TOKEN__ ?? "";
  return token ? { "X-Markbot-Session-Token": token } : {};
}

function authFetch(url: string, opts?: RequestInit): Promise<Response> {
  return fetch(url, { ...opts, headers: { ...authHeaders(), ...opts?.headers } });
}

const RECONNECT_DELAY = 3000;
const MAX_RECONNECT_ATTEMPTS = 10;

export function useChat() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [isStreaming, setIsStreaming] = useState(false);
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null);
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const wsRef = useRef<WebSocket | null>(null);
  const streamBufferRef = useRef("");
  const currentMsgIdRef = useRef<string | null>(null);
  const activeToolCallsRef = useRef<Map<string, ToolCallInfo>>(new Map());
  const reconnectAttemptsRef = useRef(0);
  const intentionalCloseRef = useRef(false);
  const currentSessionIdRef = useRef<string | null>(null);

  const setCurrentSessionIdWithRef = useCallback((id: string | null) => {
    currentSessionIdRef.current = id;
    setCurrentSessionId(id);
  }, []);

  const fetchSessions = useCallback(() => {
    authFetch("/api/sessions")
      .then((r) => r.json())
      .then((data) => {
        const list: SessionInfo[] = (data.sessions || []).map((s: any) => ({
          id: s.id,
          title: s.title,
          messageCount: s.message_count,
          lastActive: s.last_active,
        }));
        setSessions(list);
      })
      .catch(() => {});
  }, []);

  const connect = useCallback(() => {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const token = authHeaders()["X-Markbot-Session-Token"] || "";
    const ws = new WebSocket(
      `${proto}//${window.location.host}/api/ws/chat?token=${token}`
    );

    ws.onopen = () => {
      reconnectAttemptsRef.current = 0;
    };

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);

        switch (data.type) {
          case "session": {
            setCurrentSessionIdWithRef(data.session_id);
            fetchSessions();
            break;
          }

          case "session_cleared": {
            setCurrentSessionIdWithRef(null);
            fetchSessions();
            break;
          }

          case "stream_delta": {
            streamBufferRef.current += data.delta;
            const bufId = currentMsgIdRef.current;
            if (bufId) {
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === bufId
                    ? {
                        ...m,
                        content: streamBufferRef.current,
                        streaming: true,
                        toolCalls: Array.from(activeToolCallsRef.current.values()),
                      }
                    : m
                )
              );
            }
            break;
          }

          case "stream_end": {
            const endId = currentMsgIdRef.current;
            if (endId) {
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === endId
                    ? {
                        ...m,
                        streaming: false,
                        toolCalls: Array.from(activeToolCallsRef.current.values()),
                        media: data.media,
                      }
                    : m
                )
              );
            }
            setIsStreaming(false);
            currentMsgIdRef.current = null;
            streamBufferRef.current = "";
            activeToolCallsRef.current.clear();
            fetchSessions();
            break;
          }

          case "message": {
            const msgId = generateId();
            setMessages((prev) => [
              ...prev,
              {
                id: msgId,
                role: "assistant",
                content: data.content,
                timestamp: Date.now(),
                toolCalls: Array.from(activeToolCallsRef.current.values()),
                media: data.media,
              },
            ]);
            setIsStreaming(false);
            activeToolCallsRef.current.clear();
            fetchSessions();
            break;
          }

          case "tool_start": {
            const tc: ToolCallInfo = {
              toolId: data.tool_id,
              name: data.name,
              context: data.context,
              status: "running",
            };
            activeToolCallsRef.current.set(data.tool_id, tc);
            const bufId = currentMsgIdRef.current;
            if (bufId) {
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === bufId
                    ? {
                        ...m,
                        toolCalls: Array.from(activeToolCallsRef.current.values()),
                      }
                    : m
                )
              );
            }
            break;
          }

          case "tool_complete": {
            const existing = activeToolCallsRef.current.get(data.tool_id);
            if (existing) {
              existing.status = data.error ? "error" : "completed";
              existing.summary = data.summary;
              existing.error = data.error;
            } else {
              const tc: ToolCallInfo = {
                toolId: data.tool_id,
                name: data.name,
                status: data.error ? "error" : "completed",
                summary: data.summary,
                error: data.error,
              };
              activeToolCallsRef.current.set(data.tool_id, tc);
            }
            const bufId2 = currentMsgIdRef.current;
            if (bufId2) {
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === bufId2
                    ? {
                        ...m,
                        toolCalls: Array.from(activeToolCallsRef.current.values()),
                      }
                    : m
                )
              );
            }
            break;
          }

          case "progress": {
            break;
          }

          case "error": {
            const errId = generateId();
            setMessages((prev) => [
              ...prev,
              {
                id: errId,
                role: "assistant",
                content: `错误: ${data.content}`,
                timestamp: Date.now(),
              },
            ]);
            setIsStreaming(false);
            currentMsgIdRef.current = null;
            streamBufferRef.current = "";
            activeToolCallsRef.current.clear();
            break;
          }
        }
      } catch {
        // ignore
      }
    };

    ws.onclose = () => {
      wsRef.current = null;
      if (!intentionalCloseRef.current && reconnectAttemptsRef.current < MAX_RECONNECT_ATTEMPTS) {
        reconnectAttemptsRef.current += 1;
        const delay = Math.min(RECONNECT_DELAY * reconnectAttemptsRef.current, 30000);
        setTimeout(() => connect(), delay);
      }
    };

    wsRef.current = ws;
  }, [fetchSessions]);

  useEffect(() => {
    connect();
    fetchSessions();
    return () => {
      intentionalCloseRef.current = true;
      wsRef.current?.close();
    };
  }, [connect, fetchSessions]);

  const sendMessage = useCallback(async (content: string, files?: File[]) => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;

    let mediaUrls: string[] = [];
    if (files && files.length > 0) {
      for (const file of files) {
        const formData = new FormData();
        formData.append("file", file);
        try {
          const res = await authFetch("/api/upload", { method: "POST", body: formData });
          if (res.ok) {
            const data = await res.json();
            mediaUrls.push(data.url);
          }
        } catch { /* ignore single file failure */ }
      }
    }

    const userMsg: Message = {
      id: generateId(),
      role: "user",
      content: content || "(附件)",
      timestamp: Date.now(),
      media: mediaUrls.length > 0 ? mediaUrls : undefined,
    };
    setMessages((prev) => [...prev, userMsg]);

    const assistantId = generateId();
    currentMsgIdRef.current = assistantId;
    streamBufferRef.current = "";
    activeToolCallsRef.current.clear();
    setMessages((prev) => [
      ...prev,
      {
        id: assistantId,
        role: "assistant",
        content: "",
        timestamp: Date.now(),
        streaming: true,
        toolCalls: [],
      },
    ]);

    setIsStreaming(true);
    const payload: Record<string, any> = { content };
    if (mediaUrls.length > 0) payload.media = mediaUrls;
    ws.send(JSON.stringify(payload));
  }, []);

  const clearMessages = useCallback(() => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "new_session" }));
    }
    setMessages([]);
    streamBufferRef.current = "";
    currentMsgIdRef.current = null;
    setIsStreaming(false);
    activeToolCallsRef.current.clear();
    setCurrentSessionIdWithRef(null);
    fetchSessions();
  }, [fetchSessions]);

  const switchSession = useCallback((sessionId: string) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "resume_session", session_id: sessionId }));
    }
    authFetch(`/api/sessions/${sessionId}`)
      .then((r) => r.json())
      .then((data) => {
        if (data.error) return;
        setCurrentSessionIdWithRef(sessionId);
        const loaded: Message[] = (data.messages || []).map((m: any) => ({
          id: generateId(),
          role: m.role,
          content: m.content,
          timestamp: (m.timestamp || 0) * 1000,
        }));
        setMessages(loaded);
        streamBufferRef.current = "";
        currentMsgIdRef.current = null;
        setIsStreaming(false);
        activeToolCallsRef.current.clear();
      })
      .catch(() => {});
  }, []);

  const deleteSession = useCallback((sessionId: string) => {
    authFetch(`/api/sessions/${sessionId}`, { method: "DELETE" })
      .then(() => fetchSessions())
      .catch(() => {});
    if (sessionId === currentSessionIdRef.current) {
      clearMessages();
    }
  }, [clearMessages, fetchSessions]);

  return {
    messages,
    sendMessage,
    isStreaming,
    clearMessages,
    currentSessionId,
    sessions,
    switchSession,
    deleteSession,
  };
}
