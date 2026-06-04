import { ElMessageBox } from 'element-plus'

export function confirmCenteredDelete(message, title) {
  return ElMessageBox.confirm(message, title, {
    confirmButtonText: '删除',
    cancelButtonText: '取消',
    type: 'warning',
    draggable: false,
    appendTo: 'body',
    customClass: 'center-delete-dialog',
    showClose: false,
  })
}
