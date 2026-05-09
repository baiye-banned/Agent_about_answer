import request from './request'

export const authAPI = {
  login(data) {
    return request.post('/auth/login', data, { silent: true })
  },
  logout() {
    return request.post('/auth/logout')
  },
}
