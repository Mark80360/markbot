import { useCallback, useEffect, useRef, useState } from "react";
import type { Message, SessionInfo, ToolCallInfo } from "@/App";

function generateId(): string {
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
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
    fetch("/api/sessions")
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

    fetch("/api/status")
      .then((r) => r.json())
      .then((data) => {
        const token = data.token;
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
      })
      .catch(() => {
        if (reconnectAttemptsRef.current < MAX_RECONNECT_ATTEMPTS) {
          reconnectAttemptsRef.current += 1;
          const delay = Math.min(RECONNECT_DELAY * reconnectAttemptsRef.current, 30000);
          setTimeout(() => connect(), delay);
        }
      });
  }, [fetchSessions]);

  useEffect(() => {
    connect();
    fetchSessions();
    return () => {
      intentionalCloseRef.current = true;
      wsRef.current?.close();
    };
  }, [connect, fetchSessions]);

  const sendMessage = useCallback((content: string) => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;

    const userMsg: Message = {
      id: generateId(),
      role: "user",
      content,
      timestamp: Date.now(),
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
    ws.send(JSON.stringify({ content }));
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
    fetch(`/api/sessions/${sessionId}`)
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
    fetch(`/api/sessions/${sessionId}`, { method: "DELETE" })
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
