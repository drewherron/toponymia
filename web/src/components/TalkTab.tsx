import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  createTalkThread,
  deleteTalkPost,
  deleteTalkThread,
  editTalkPost,
  getTalk,
  replyTalkThread,
} from '../api'
import type { TalkPost, TalkThread, User } from '../types'
import ReportButton from './ReportButton'

interface TalkTabProps {
  slug: string
  user: User | null
  onRequestAuth: () => void
}

const plugins = [remarkGfm]

function formatWhen(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  })
}

function PostView({
  post,
  user,
  onChanged,
}: {
  post: TalkPost
  user: User | null
  onChanged: (post: TalkPost) => void
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)

  const own = user?.username === post.author
  const canModerate = user?.is_moderator ?? false

  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (!draft.trim()) return
    setBusy(true)
    editTalkPost(post.id, draft)
      .then((updated) => {
        onChanged(updated)
        setEditing(false)
      })
      .catch(console.error)
      .finally(() => setBusy(false))
  }

  const remove = () => {
    if (!window.confirm('Remove this post?')) return
    setBusy(true)
    deleteTalkPost(post.id)
      .then(onChanged)
      .catch(console.error)
      .finally(() => setBusy(false))
  }

  if (post.deleted) {
    return (
      <div className="talk-post talk-post-deleted">
        <p className="talk-post-byline">
          <strong>{post.author}</strong> · {formatWhen(post.created)}
        </p>
        <p className="talk-post-tombstone">[post removed]</p>
      </div>
    )
  }

  return (
    <div className="talk-post">
      <p className="talk-post-byline">
        <strong>{post.author}</strong> · {formatWhen(post.created)}
        {post.edited && <span className="talk-post-edited">edited</span>}
        {own && !editing && (
          <button
            type="button"
            className="talk-post-edit"
            onClick={() => {
              setDraft(post.body_md)
              setEditing(true)
            }}
          >
            edit
          </button>
        )}
        {(own || canModerate) && !editing && (
          <button
            type="button"
            className="talk-post-delete"
            disabled={busy}
            onClick={remove}
          >
            delete
          </button>
        )}
        {!own && !editing && (
          <ReportButton
            targetType="talk_post"
            targetId={post.id}
            loggedIn={!!user}
          />
        )}
      </p>
      {editing ? (
        <form className="talk-form" onSubmit={submit}>
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            rows={3}
          />
          <div className="talk-form-actions">
            <button type="submit" disabled={busy || !draft.trim()}>
              Save
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => setEditing(false)}
            >
              Cancel
            </button>
          </div>
        </form>
      ) : (
        <div className="talk-post-body">
          <Markdown remarkPlugins={plugins}>{post.body_md}</Markdown>
        </div>
      )}
    </div>
  )
}

function ThreadView({
  thread,
  user,
  onRequestAuth,
  onChanged,
  onDeleted,
}: {
  thread: TalkThread
  user: User | null
  onRequestAuth: () => void
  onChanged: (thread: TalkThread) => void
  onDeleted: (threadId: number) => void
}) {
  const [replying, setReplying] = useState(false)
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (!draft.trim()) return
    setBusy(true)
    replyTalkThread(thread.id, draft)
      .then((post) => {
        onChanged({ ...thread, posts: [...thread.posts, post] })
        setDraft('')
        setReplying(false)
      })
      .catch(console.error)
      .finally(() => setBusy(false))
  }

  const handlePostChanged = (updated: TalkPost) => {
    onChanged({
      ...thread,
      posts: thread.posts.map((post) =>
        post.id === updated.id ? updated : post,
      ),
    })
  }

  const removeThread = () => {
    if (!window.confirm('Remove this whole thread?')) return
    setBusy(true)
    deleteTalkThread(thread.id)
      .then(() => onDeleted(thread.id))
      .catch(console.error)
      .finally(() => setBusy(false))
  }

  return (
    <section className="talk-thread">
      <h3>
        {thread.title}
        {user?.is_moderator && (
          <button
            type="button"
            className="talk-thread-delete"
            disabled={busy}
            onClick={removeThread}
          >
            delete thread
          </button>
        )}
      </h3>
      {thread.posts.map((post) => (
        <PostView
          key={post.id}
          post={post}
          user={user}
          onChanged={handlePostChanged}
        />
      ))}
      {replying ? (
        <form className="talk-form" onSubmit={submit}>
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            rows={3}
            placeholder="Your reply (Markdown)"
          />
          <div className="talk-form-actions">
            <button type="submit" disabled={busy || !draft.trim()}>
              Post reply
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => setReplying(false)}
            >
              Cancel
            </button>
          </div>
        </form>
      ) : (
        <button
          type="button"
          className="talk-reply-button"
          onClick={() => (user ? setReplying(true) : onRequestAuth())}
        >
          Reply
        </button>
      )}
    </section>
  )
}

function TalkTab({ slug, user, onRequestAuth }: TalkTabProps) {
  const [threads, setThreads] = useState<TalkThread[] | null>(null)
  const [error, setError] = useState(false)
  const [composing, setComposing] = useState(false)
  const [title, setTitle] = useState('')
  const [body, setBody] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    const controller = new AbortController()
    setError(false)
    getTalk(slug, controller.signal)
      .then(setThreads)
      .catch((err: unknown) => {
        if (!controller.signal.aborted) {
          console.error(err)
          setError(true)
        }
      })
    return () => controller.abort()
  }, [slug])

  const submitThread = (event: FormEvent) => {
    event.preventDefault()
    if (!title.trim() || !body.trim()) return
    setBusy(true)
    createTalkThread(slug, title.trim(), body)
      .then((thread) => {
        setThreads((prev) => [...(prev ?? []), thread])
        setTitle('')
        setBody('')
        setComposing(false)
      })
      .catch(console.error)
      .finally(() => setBusy(false))
  }

  const handleThreadChanged = (updated: TalkThread) => {
    setThreads((prev) =>
      prev
        ? prev.map((thread) =>
            thread.id === updated.id ? updated : thread,
          )
        : prev,
    )
  }

  const handleThreadDeleted = (threadId: number) => {
    setThreads((prev) =>
      prev ? prev.filter((thread) => thread.id !== threadId) : prev,
    )
  }

  if (error) {
    return <p className="feature-pane-note">Could not load discussions.</p>
  }
  if (threads === null) {
    return <p className="feature-pane-note">Loading discussions…</p>
  }

  return (
    <div className="talk">
      {threads.length === 0 && (
        <p className="feature-pane-note">
          No discussions about this place yet.
        </p>
      )}
      {threads.map((thread) => (
        <ThreadView
          key={thread.id}
          thread={thread}
          user={user}
          onRequestAuth={onRequestAuth}
          onChanged={handleThreadChanged}
          onDeleted={handleThreadDeleted}
        />
      ))}
      {composing ? (
        <form className="talk-form talk-new-thread" onSubmit={submitThread}>
          <label>
            Topic
            <input
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              maxLength={255}
              placeholder="What is this discussion about?"
            />
          </label>
          <textarea
            value={body}
            onChange={(e) => setBody(e.target.value)}
            rows={4}
            placeholder="Your message (Markdown)"
          />
          <div className="talk-form-actions">
            <button
              type="submit"
              disabled={busy || !title.trim() || !body.trim()}
            >
              Start discussion
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => setComposing(false)}
            >
              Cancel
            </button>
          </div>
        </form>
      ) : (
        <button
          type="button"
          className="talk-new-button"
          onClick={() => (user ? setComposing(true) : onRequestAuth())}
        >
          {user ? 'Start a discussion' : 'Log in to start a discussion'}
        </button>
      )}
    </div>
  )
}

export default TalkTab
