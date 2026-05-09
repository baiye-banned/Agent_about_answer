import request from './request'

export const userAPI = {
  getProfile() {
    return request.get('/user/profile')
  },
  updatePassword(data) {
    return request.put('/user/password', data, { silent: true })
  },
  uploadAvatar(file) {
    const formData = new FormData()
    formData.append('file', file)
    return request.post('/user/avatar', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
  },
}
