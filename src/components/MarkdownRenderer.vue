<template>
  <div class="markdown-body" v-html="rendered" />
</template>

<script setup>
import { computed } from 'vue'
import { marked } from 'marked'
import { markedHighlight } from 'marked-highlight'
import hljs from 'highlight.js'
import 'highlight.js/styles/github.css'

marked.use(
  markedHighlight({
    langPrefix: 'hljs language-',
    highlight(code, lang) {
      if (lang && hljs.getLanguage(lang)) {
        return hljs.highlight(code, { language: lang }).value
      }
      return hljs.highlightAuto(code).value
    },
  })
)

marked.setOptions({
  breaks: true,
  gfm: true,
})

const props = defineProps({
  content: {
    type: String,
    default: '',
  },
})

const rendered = computed(() => sanitizeHtml(marked.parse(props.content || '')))

function sanitizeHtml(html) {
  if (typeof document === 'undefined') return html

  const template = document.createElement('template')
  template.innerHTML = html

  template.content.querySelectorAll('script, iframe, object, embed, style, link').forEach((node) => {
    node.remove()
  })

  template.content.querySelectorAll('*').forEach((node) => {
    for (const attr of [...node.attributes]) {
      const name = attr.name.toLowerCase()
      const value = attr.value.trim().toLowerCase()
      const isUnsafeUrl = ['href', 'src'].includes(name) && value.startsWith('javascript:')

      if (name.startsWith('on') || isUnsafeUrl) {
        node.removeAttribute(attr.name)
      }
    }
  })

  return template.innerHTML
}
</script>

<style scoped>
.markdown-body {
  line-height: 1.7;
  color: #334155;
  font-size: 14px;
}

.markdown-body :deep(p) {
  margin: 0.45em 0;
}

.markdown-body :deep(ul),
.markdown-body :deep(ol) {
  padding-left: 1.4em;
  margin: 0.45em 0;
}

.markdown-body :deep(pre) {
  background: #f1f5f9;
  border: 1px solid #e2e8f0;
  padding: 1em;
  border-radius: 6px;
  overflow-x: auto;
}

.markdown-body :deep(code) {
  font-size: 0.875em;
}

.markdown-body :deep(:not(pre) > code) {
  background: #f1f5f9;
  padding: 0.1em 0.35em;
  border-radius: 4px;
}

.markdown-body :deep(table) {
  border-collapse: collapse;
  width: 100%;
  margin: 0.75em 0;
}

.markdown-body :deep(th),
.markdown-body :deep(td) {
  border: 1px solid #cbd5e1;
  padding: 0.5em;
}

.markdown-body :deep(blockquote) {
  border-left: 3px solid #60a5fa;
  padding-left: 1em;
  color: #64748b;
}
</style>
