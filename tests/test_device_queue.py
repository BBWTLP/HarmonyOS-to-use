import threading
import time
import unittest
from harmony_runtime.device_queue import DeviceQueue

class DeviceQueueTests(unittest.TestCase):
    def wait_pending(self, queue, count):
        end=time.monotonic()+2
        while queue.pending != count and time.monotonic()<end:
            time.sleep(.002)
        self.assertEqual(queue.pending,count)

    def test_arrival_order_survives_polling_and_timeout_removal(self):
        queue=DeviceQueue();result=[];threads=[]
        queue.acquire()
        def run(value,timeout):
            if queue.acquire(timeout=timeout):
                try:result.append(value)
                finally:queue.release()
            else:result.append("expired")
        try:
            for index,timeout in [(0,2),(1,.15),(2,2)]:
                thread=threading.Thread(target=run,args=(index,timeout))
                threads.append(thread);thread.start()
                self.wait_pending(queue,index+1)
            threads[1].join(1)
            self.assertFalse(threads[1].is_alive())
            self.wait_pending(queue,2)
        finally:
            queue.release()
            for thread in threads:thread.join(3)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(result,["expired",0,2])
        self.assertEqual(queue.pending,0)

    def test_cancelled_ticket_does_not_block_successor(self):
        queue=DeviceQueue();cancel=threading.Event();errors=[]
        queue.acquire()
        def check():
            if cancel.is_set():raise ValueError("cancelled")
        def run():
            try:queue.acquire(timeout=2,check=check)
            except ValueError as exc:errors.append(str(exc))
        thread=threading.Thread(target=run);thread.start()
        try:
            self.wait_pending(queue,1);cancel.set();thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors,["cancelled"])
            self.assertEqual(queue.pending,0)
        finally:queue.release();thread.join(2)
        with queue:pass

if __name__=="__main__":unittest.main()
