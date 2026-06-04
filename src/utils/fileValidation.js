export const ACCEPTED_IMAGE_MIME_TYPES = ['image/png', 'image/jpeg', 'image/webp']
export const ACCEPTED_IMAGE_INPUT = ACCEPTED_IMAGE_MIME_TYPES.join(',')

export function validateImageFile(file, options = {}) {
  const { maxSizeMb, typeMessage, sizeMessage } = options
  if (!ACCEPTED_IMAGE_MIME_TYPES.includes(file?.type)) {
    return typeMessage || '仅支持 png、jpg、jpeg、webp 图片'
  }

  const maxBytes = Number.isFinite(Number(maxSizeMb)) ? Number(maxSizeMb) * 1024 * 1024 : null
  const fileSize = Number(file?.size)
  if (maxBytes !== null && (!Number.isFinite(fileSize) || fileSize > maxBytes)) {
    return sizeMessage || `图片不能超过 ${maxSizeMb}MB`
  }

  return ''
}
