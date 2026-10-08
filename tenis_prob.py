import sys
from functools import lru_cache
def prob(pa,pb,sa,sb,ga,gb,a_srv,xa=0,xb=0,bo=3):
    need=bo//2+1
    def hold(p,x,y):
        @lru_cache(None)
        def g(x,y):
            if x>=4 and x-y>=2: return 1.0
            if y>=4 and y-x>=2: return 0.0
            if x>=3 and y>=3 and x==y: return p*p/(p*p+(1-p)**2)
            if x>=3 and y>=3: return p+(1-p)*g(3,3) if x>y else p*g(3,3)
            return p*g(x+1,y)+(1-p)*g(x,y+1)
        return g(x,y)
    @lru_cache(None)
    def tb(x,y,a_now):
        if x>=7 and x-y>=2: return 1.0
        if y>=7 and y-x>=2: return 0.0
        if x>=6 and y>=6 and x==y:
            w=pa*(1-pb); l=(1-pa)*pb; return w/(w+l)
        p=pa if a_now else 1-pb
        n=x+y+1
        swap=(n%2==1)
        a2=(not a_now) if swap else a_now
        return p*tb(x+1,y,a2)+(1-p)*tb(x,y+1,a2)
    ha,hb=hold(pa,0,0),hold(pb,0,0)
    @lru_cache(None)
    def S(x,y,a):
        if (x>=6 and x-y>=2) or x==7: return 1.0
        if (y>=6 and y-x>=2) or y==7: return 0.0
        if x==6 and y==6: return tb(0,0,a)
        p=ha if a else 1-hb
        return p*S(x+1,y,not a)+(1-p)*S(x,y+1,not a)
    fs=S(0,0,True)
    @lru_cache(None)
    def W(a,b):
        if a==need: return 1.0
        if b==need: return 0.0
        return fs*W(a+1,b)+(1-fs)*W(a,b+1)
    if ga==6 and gb==6: s=tb(xa,xb,a_srv)
    elif a_srv:
        h=hold(pa,xa,xb); s=h*S(ga+1,gb,False)+(1-h)*S(ga,gb+1,False)
    else:
        h=hold(pb,xb,xa); s=h*S(ga,gb+1,True)+(1-h)*S(ga+1,gb,True)
    return s*W(sa+1,sb)+(1-s)*W(sa,sb+1)
if __name__=="__main__":
    a=sys.argv[1:]
    pa,pb=float(a[0]),float(a[1]); sa,sb,ga,gb=map(int,a[2:6]); srv=a[6].upper()=="A"
    xa=int(a[7]) if len(a)>7 else 0; xb=int(a[8]) if len(a)>8 else 0; bo=int(a[9]) if len(a)>9 else 3
    print(f"P(A gana) = {prob(pa,pb,sa,sb,ga,gb,srv,xa,xb,bo)*100:.1f}%")
