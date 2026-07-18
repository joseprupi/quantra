import { expect, test } from 'vitest'
import { render, screen } from '@testing-library/react'

function Hello() {
  return <span>hello world</span>
}

test('smoke: component renders', () => {
  render(<Hello />)
  const el = screen.getByText('hello world')
  expect(el).toBeInTheDocument()
  expect(el).toHaveTextContent('hello world')
})
